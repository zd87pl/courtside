"""courtside CLI - segment a session video, analyze clips with a local VLM,
and write a coaching report (Markdown + self-contained HTML).

Examples
--------
# quick plumbing check, no model load:
courtside match.mp4 --dry-run

# default run (Qwen3-VL-32B 8-bit, auto rally detection):
courtside match.mp4

# fast smoke test on the first 3 clips with the 8B model:
courtside match.mp4 --model qwen3-vl-8b --max-clips 3

# provably offline (airplane-mode demo): weights must already be cached
courtside match.mp4 --offline

# re-render reports from a previous run with ZERO model calls (instant demo):
courtside --from-dir match_courtside

# resume a crashed run, skipping clips already analyzed:
courtside match.mp4 --resume

# against a running server (mlx_vlm.server / LM Studio), with strict schema:
courtside match.mp4 --server-url http://localhost:8080/v1 --server-model Qwen/Qwen3-VL-32B-Instruct
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from . import __version__, config, report
from .fetch import fetch_video, is_url
from .frames import ClipFrames, extract_clip_frames, probe_duration, require_tools
from .prompts import SYSTEM, build_clip_prompt, build_report_prompt, limitations_footer
from .report_html import write_html_report
from .schema import CANONICAL_FLAG_CODES, ClipAnalysis, clip_json_schema
from .segment import ActivityCurve, Segment, _activity_curve, detect_segments, fixed_windows, from_file
from .vlm import LocalVLM, ServerVLM, extract_json, strip_think

_FLAG_VOCAB = ", ".join(sorted(CANONICAL_FLAG_CODES))


def _log(msg: str) -> None:
    print(msg, flush=True)


def _clamp_timestamps(analysis: ClipAnalysis, seg: Segment) -> int:
    """Clamp each stroke's t_s into the clip window; return #corrections.

    Guards the classic VLM clip-relative-vs-absolute confusion, which otherwise
    poisons report citations (finding: Stroke.t_s unvalidated)."""
    fixed = 0
    lo, hi = seg.start_s, seg.end_s
    for st in analysis.strokes:
        if st.t_s < lo or st.t_s > hi:
            st.t_s = min(max(st.t_s, lo), hi)
            fixed += 1
    return fixed


def _snippet(text: str) -> str:
    """A short, readable preview of raw model output for failure logs."""
    s = (text or "").strip()
    return repr(s[:280] + (" ..." if len(s) > 280 else "")) if s else "<empty output>"


def analyze_clip(vlm, clip_frames: ClipFrames, schema: dict, max_tokens: int,
                 stream: bool = False, raw_path: Path | None = None) -> tuple[ClipAnalysis, dict]:
    seg = clip_frames.segment
    prompt = SYSTEM + "\n\n" + build_clip_prompt(
        n_frames=len(clip_frames.frames),
        start_s=seg.start_s,
        end_s=seg.end_s,
        schema=schema,
        timestamps=clip_frames.timestamps(),
        flag_vocab=_FLAG_VOCAB,
    )
    on_token = (lambda t: print(t, end="", flush=True)) if stream else None
    text, stats = vlm.generate(
        prompt, images=clip_frames.frames, max_tokens=max_tokens,
        temperature=0.0, json_schema=schema, on_token=on_token,
    )
    if stream:
        print(flush=True)
    try:
        parsed = ClipAnalysis.model_validate(extract_json(text))
    except (ValueError, ValidationError) as e:
        # surface what the model actually produced - saved to disk and logged so
        # a parse failure is diagnosable instead of opaque
        if raw_path is not None:
            try:
                raw_path.write_text(text or "")
            except OSError:
                pass
        _log(f"    ! invalid JSON ({type(e).__name__}); model said: {_snippet(text)}")
        # one repair round: keep the frames AND the schema in view so the model
        # can actually correct against them.
        repair = (
            "Your previous output was invalid JSON for the required schema.\n"
            f"Error: {e}\n\nRequired JSON schema:\n{json.dumps(schema)}\n\n"
            f"Previous output:\n{strip_think(text)[:2000]}\n\n"
            "Look at the frames again and return ONLY the corrected JSON object, nothing else."
        )
        text2, stats2 = vlm.generate(repair, images=clip_frames.frames, max_tokens=max_tokens,
                                     temperature=0.0, json_schema=schema)
        stats.wall_s += stats2.wall_s
        try:
            parsed = ClipAnalysis.model_validate(extract_json(text2))
        except (ValueError, ValidationError) as e2:
            if raw_path is not None:
                try:
                    raw_path.with_suffix(".repair.txt").write_text(text2 or "")
                except OSError:
                    pass
            raise ValueError(
                f"model did not return valid JSON after one repair "
                f"({type(e2).__name__}). repair output: {_snippet(text2)}"
            ) from e2
    # trust the pipeline's clip boundaries over the model's
    parsed.start_s, parsed.end_s = seg.start_s, seg.end_s
    n_fixed = _clamp_timestamps(parsed, seg)
    if n_fixed:
        _log(f"    clamped {n_fixed} out-of-window timestamp(s)")
    return parsed, {
        "wall_s": round(stats.wall_s, 2),
        "prompt_tokens": stats.prompt_tokens,
        "generation_tokens": stats.generation_tokens,
        "prompt_tps": round(stats.prompt_tps, 1) if stats.prompt_tps else None,
        "generation_tps": round(stats.generation_tps, 1) if stats.generation_tps else None,
        "peak_gb": round(stats.peak_gb, 2) if stats.peak_gb else None,
    }


def _model_is_cached(repo: str) -> bool:
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo, local_files_only=True)
        return True
    except Exception:
        return False


def _preflight_model(repo: str, key: str, offline: bool) -> None:
    """Warn about a cold cache / insufficient RAM before spending minutes on
    segmentation + extraction (findings: silent 35GB download; no RAM pre-flight)."""
    weights = config.model_weights_gb(key)
    ram = config.total_ram_gb()
    if ram and weights and weights * 1.3 > ram:
        _log(f"  ! WARNING: {key} needs ~{weights:.0f} GB weights; this machine has "
             f"~{ram:.0f} GB RAM. Expect Metal OOM or swapping. Consider --model qwen3-vl-8b.")
    if not _model_is_cached(repo):
        size = f"~{weights:.0f} GB" if weights else "several GB"
        if offline:
            raise SystemExit(
                f"--offline set but '{repo}' is not in the local Hugging Face cache. "
                f"Pre-fetch it first: courtside --prefetch {key}"
            )
        _log(f"  ! '{repo}' is not cached; the first run will download {size} from Hugging Face.")


def _prefetch(repo: str) -> int:
    from huggingface_hub import snapshot_download
    _log(f"prefetching {repo} ...")
    snapshot_download(repo)
    _log("done - weights are cached; you can now run --offline.")
    return 0


# ---------------- report building ----------------

def _generate_markdown(vlm, analyses: list[ClipAnalysis], facts: dict, res_s: float) -> str:
    """Model pass that writes the prose coaching report (Markdown)."""
    _log("generating session report ...")
    session_dicts = [a.model_dump() for a in analyses]
    report_text, _ = vlm.generate(
        SYSTEM + "\n\n" + build_report_prompt(session_dicts, facts),
        images=None, max_tokens=2400, temperature=0.4,
    )
    return strip_think(report_text).strip() + "\n" + limitations_footer(res_s)


def _finalize_reports(out_dir: Path, session_doc: dict, markdown: str | None) -> None:
    """Persist session.json, the Markdown report, and the self-contained HTML."""
    if markdown is not None:
        (out_dir / "session_report.md").write_text(markdown)
    (out_dir / "session.json").write_text(json.dumps(session_doc, indent=2))
    html_path = write_html_report(out_dir, session_doc)
    _log(f"reports written: {out_dir / 'session_report.md'} + {html_path.name}")


def _replay(out_dir: Path) -> int:
    """Rebuild session.json + HTML from cached clip data - zero model calls."""
    doc_path = out_dir / "session.json"
    if not doc_path.exists():
        _log(f"no session.json in {out_dir} - nothing to replay")
        return 1
    session_doc = json.loads(doc_path.read_text())
    # session.json may predate the envelope format (bare list); wrap minimally.
    if isinstance(session_doc, list):
        analyses = [ClipAnalysis.model_validate(c) for c in session_doc]
        facts = report.compute_session_facts(analyses)
        session_doc = report.build_session_doc(
            version=__version__, video_name=out_dir.name, video_duration_s=0.0,
            model="(unknown)", backend="local", settings={}, created_at=None,
            run_stats={"total_tokens": None}, facts=facts,
            clips=[{"index": i, "frame_dir": f"clip_{i:03d}", "fps_used": config.DEFAULT_FPS,
                    "status": "ok", "analysis": a.model_dump()} for i, a in enumerate(analyses)],
        )
    html_path = write_html_report(out_dir, session_doc)
    _log(f"replayed {out_dir} -> {html_path} (no model loaded)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="courtside", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", nargs="?",
                   help="local video file, or a YouTube/any yt-dlp-supported URL")
    p.add_argument("--model", default=config.DEFAULT_MODEL_KEY,
                   help=f"registry key or HF repo/local path (default: {config.DEFAULT_MODEL_KEY}; "
                        f"keys: {', '.join(config.MODELS)})")
    p.add_argument("--segment", choices=["auto", "fixed", "file"], default="auto")
    p.add_argument("--window", type=float, default=20.0, help="window seconds for --segment fixed")
    p.add_argument("--segments-file", type=Path, help="JSON segments for --segment file")
    p.add_argument("--fps", type=float, default=config.DEFAULT_FPS)
    p.add_argument("--max-frames", type=int, default=config.DEFAULT_MAX_FRAMES)
    p.add_argument("--max-side", type=int, default=config.DEFAULT_MAX_SIDE)
    p.add_argument("--max-clips", type=int, default=0, help="limit clips (0 = all)")
    p.add_argument("--max-tokens", type=int, default=1400)
    p.add_argument("--kv-bits", type=int, default=None, help="KV cache quantization bits (e.g. 4 or 8)")
    p.add_argument("--out", type=Path, default=None, help="output dir (default: <video>_courtside)")
    p.add_argument("--dry-run", action="store_true", help="segment + extract frames only, no model")
    p.add_argument("--resume", action="store_true", help="skip clips whose clip_NNN.json already exists")
    p.add_argument("--from-dir", type=Path, default=None,
                   help="re-render reports from a previous run dir (no model)")
    p.add_argument("--prefetch", metavar="MODEL", default=None,
                   help="download a model's weights to the HF cache and exit")
    p.add_argument("--download-dir", type=Path, default=None,
                   help="where URL inputs are downloaded (default: current directory)")
    p.add_argument("--offline", action="store_true",
                   help="forbid any network access (weights must be pre-cached)")
    p.add_argument("--stream", action="store_true", help="echo model tokens live during analysis")
    p.add_argument("--moments", type=int, default=6, metavar="N",
                   help="deep-dive the top N flagged strokes with slow-mo + biomechanics "
                        "+ coaching cards (0 disables; default 6)")
    p.add_argument("--no-pose", action="store_true",
                   help="skip pose/biomechanics even if the [pose] extra is installed")
    p.add_argument("--smooth-slowmo", action="store_true",
                   help="motion-interpolated slow-mo (nicer, much slower to render)")
    p.add_argument("--heatmap", action="store_true",
                   help="EXPERIMENTAL: map flagged-moment positions onto a court diagram "
                        "(fixed camera + [pose] extra required)")
    p.add_argument("--server-url", default=None, help="OpenAI-compatible base URL instead of local load "
                   "(e.g. https://openrouter.ai/api/v1)")
    p.add_argument("--server-model", default=None, help="model name for --server-url")
    p.add_argument("--api-key", default=None,
                   help="API key for --server-url (default: $OPENROUTER_API_KEY or $OPENAI_API_KEY)")
    args = p.parse_args(argv)

    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    if args.prefetch:
        return _prefetch(config.resolve_model(args.prefetch))

    if args.from_dir:
        return _replay(args.from_dir)

    if args.video is None:
        p.error("video is required (or use --from-dir DIR / --prefetch MODEL)")

    if is_url(args.video):
        if args.offline:
            p.error("--offline forbids downloading; pass a local file instead of a URL")
        _log(f"fetching {args.video} ...")
        try:
            video = fetch_video(args.video, args.download_dir or Path.cwd(), on_line=_log)
        except RuntimeError as e:
            _log(f"error: {e}")
            return 2
        _log(f"downloaded: {video.name}")
    else:
        video = Path(args.video)
    if not video.exists():
        p.error(f"video not found: {video}")
    # absolute path: ffmpeg/ffprobe/cv2 would parse a leading-dash filename as
    # a flag (pathlib normalizes "./-x.mp4" to "-x.mp4", so "./" doesn't help)
    video = video.resolve()

    try:
        require_tools()
    except RuntimeError as e:
        _log(f"error: {e}")
        return 2

    out_dir = args.out or video.with_name(video.stem + "_courtside")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(video)
    _log(f"video: {video.name}  ({duration/60:.1f} min)")

    # true end-to-end wall clock starts here (segmentation onward)
    pipeline_t0 = time.perf_counter()

    # 1) segment (reuse the activity curve for the report timeline)
    t0 = time.perf_counter()
    curve: ActivityCurve | None = None
    if args.segment == "auto":
        curve = _activity_curve(video)
        segments = detect_segments(video, min_rally_s=config.DEFAULT_MIN_RALLY_S,
                                   max_clip_s=config.DEFAULT_MAX_CLIP_S, curve=curve)
        if not segments:
            _log("  auto-segmentation found nothing usable; falling back to fixed 20s windows")
            segments = fixed_windows(video, 20.0)
    elif args.segment == "fixed":
        segments = fixed_windows(video, args.window)
    else:
        if not args.segments_file:
            p.error("--segment file requires --segments-file")
        segments = from_file(args.segments_file)
    if args.max_clips:
        segments = segments[: args.max_clips]
    segment_s = time.perf_counter() - t0
    _log(f"segments: {len(segments)} active clips "
         f"({sum(s.duration for s in segments):.0f}s of play, {segment_s:.1f}s to detect)")
    if not segments:
        _log("no segments found - try --segment fixed --window 15")
        return 1
    if curve is not None:
        (out_dir / "activity.json").write_text(json.dumps({"times": curve.times, "scores": curve.scores}))

    # 2) frames
    t_extract = time.perf_counter()
    clips: list[ClipFrames] = []
    for i, seg in enumerate(segments):
        try:
            cf = extract_clip_frames(video, seg, out_dir / f"clip_{i:03d}",
                                     fps=args.fps, max_frames=args.max_frames, max_side=args.max_side)
        except Exception as e:  # one bad segment must not abort the run
            _log(f"  clip {i:03d}  extraction FAILED: {e} - skipping")
            continue
        clips.append(cf)
        _log(f"  clip {i:03d}  {seg.start_s:7.1f}-{seg.end_s:7.1f}s  {len(cf.frames)} frames @ {cf.fps_used:.2f} fps")
    extract_s = time.perf_counter() - t_extract

    if args.dry_run:
        _log("dry run complete - frames are on disk, no model loaded")
        return 0
    if not clips:
        _log("no frames extracted - aborting")
        return 1

    # 3) VLM backend
    backend = "server" if args.server_url else "local"
    model_load_s = 0.0
    if args.server_url:
        if not args.server_model:
            p.error("--server-url requires --server-model")
        vlm = ServerVLM(args.server_url, args.server_model, api_key=args.api_key)
        try:
            vlm.ping()
        except Exception as e:
            _log(f"error: cannot reach server {args.server_url}: {e}")
            return 2
        model_name = args.server_model
        _log(f"backend: server {args.server_url}  model={model_name}")
    else:
        repo = config.resolve_model(args.model)
        _preflight_model(repo, args.model, args.offline)
        _log(f"backend: local mlx-vlm  model={repo}  (loading, this can take a minute...)")
        try:
            vlm = LocalVLM(repo, kv_bits=args.kv_bits)
        except ImportError:
            _log("error: mlx-vlm is not installed. On Apple Silicon: pip install -e '.[local]'. "
                 "Off-Mac, use --server-url against an OpenAI-compatible endpoint.")
            return 2
        model_name = repo
        model_load_s = vlm.load_s
        _log(f"loaded in {vlm.load_s:.1f}s")

    schema = clip_json_schema()
    analyses: list[ClipAnalysis] = []
    per_clip_stats: list[dict] = []
    clip_records: list[dict] = []
    clips_failed = 0
    interrupted = False
    try:
        for i, cf in enumerate(clips):
            clip_json_path = out_dir / f"clip_{i:03d}.json"
            stats_path = out_dir / f"clip_{i:03d}.stats.json"
            if args.resume and clip_json_path.exists():
                try:
                    analysis = ClipAnalysis.model_validate_json(clip_json_path.read_text())
                    analyses.append(analysis)
                    # reload cached timing/token stats so a resumed run still
                    # reports honest totals (finding: resume undercounted metrics)
                    if stats_path.exists():
                        try:
                            per_clip_stats.append(json.loads(stats_path.read_text()))
                        except (ValueError, OSError):
                            pass
                    clip_records.append({"index": i, "frame_dir": f"clip_{i:03d}",
                                         "fps_used": cf.fps_used, "n_frames": len(cf.frames),
                                         "status": "ok", "analysis": analysis.model_dump()})
                    _log(f"[{i+1}/{len(clips)}] resume: reusing {clip_json_path.name}")
                    continue
                except (ValueError, ValidationError):
                    pass  # fall through to re-analyze
            _log(f"[{i+1}/{len(clips)}] analyzing clip {cf.segment.start_s:.1f}-{cf.segment.end_s:.1f}s ...")
            try:
                analysis, stats = analyze_clip(vlm, cf, schema, args.max_tokens, stream=args.stream,
                                               raw_path=out_dir / f"clip_{i:03d}.raw.txt")
            except KeyboardInterrupt:
                interrupted = True
                _log("\n  interrupted - finishing with the clips completed so far")
                break
            except Exception as e:  # per-clip recovery: never let one clip kill the run
                clips_failed += 1
                _log(f"    ! clip {i:03d} failed: {type(e).__name__}: {e} - skipping")
                clip_records.append({"index": i, "frame_dir": f"clip_{i:03d}", "fps_used": cf.fps_used,
                                     "n_frames": len(cf.frames), "status": "failed",
                                     "error": f"{type(e).__name__}: {e}", "analysis": None})
                continue
            analyses.append(analysis)
            per_clip_stats.append(stats)
            clip_json_path.write_text(analysis.model_dump_json(indent=2))
            stats_path.write_text(json.dumps(stats))
            clip_records.append({"index": i, "frame_dir": f"clip_{i:03d}", "fps_used": cf.fps_used,
                                 "n_frames": len(cf.frames), "status": "ok", "analysis": analysis.model_dump()})
            n_flags = sum(len(s.technique_flags) + len(s.tactical_flags) for s in analysis.strokes)
            _log(f"    {len(analysis.strokes)} strokes, {n_flags} flags, conf={analysis.confidence}  "
                 f"({stats['wall_s']}s, prefill {stats['prompt_tps'] or '?'} t/s, "
                 f"decode {stats['generation_tps'] or '?'} t/s, peak {stats['peak_gb'] or '?'} GB)")

        # 4) reports - always reached, built from whatever succeeded
        if not analyses:
            _log("no clips analyzed successfully - no report generated")
            if backend == "local":
                _log(
                    "  hint: run `courtside-doctor` to isolate this in one shot (it tests\n"
                    "  1/8/32-frame generation directly and prints a specific diagnosis).\n"
                    "  Common causes: an mlx-vlm >=0.6.4 Qwen regression (workaround:\n"
                    "  pip install --force-reinstall --no-deps 'mlx-vlm==0.6.3'), or the\n"
                    f"  {args.max_frames}-frame prefill exceeding GPU memory (try --max-frames 12\n"
                    "  --max-side 672, or raise iogpu.wired_limit_mb - see README).\n"
                    "  Cloud fallback: courtside <video> --server-url https://openrouter.ai/api/v1 \\\n"
                    "    --server-model qwen/qwen2.5-vl-72b-instruct   (needs $OPENROUTER_API_KEY)\n"
                    "  raw model output for each clip was saved to clip_NNN.raw.txt."
                )
            return 1

        facts = report.compute_session_facts(analyses)
        fps_used_values = [c["fps_used"] for c in clip_records if c.get("fps_used")]
        res_s = report.frame_resolution_s(fps_used_values)

        # 4a) deep-dive moments + contact-quality sweep
        moments: list[dict] = []
        contact_quality: dict = {}
        if args.moments > 0 and not interrupted:
            from .moments import build_moments
            _log("building flagged-moment deep dives ...")
            try:
                moments, contact_quality = build_moments(
                    video, analyses, out_dir, vlm=vlm,
                    cap=args.moments, use_pose=not args.no_pose,
                    smooth_slowmo=args.smooth_slowmo, log=_log)
            except Exception as e:  # noqa: BLE001 - deep dives must never kill the report
                _log(f"  moments failed ({type(e).__name__}: {e}) - continuing without")
        if contact_quality.get("summary"):
            # the report prompt sees the measured strike-zone stats as facts
            facts["contact_quality"] = contact_quality["summary"]

        t_report = time.perf_counter()
        markdown = _generate_markdown(vlm, analyses, facts, res_s)
        report_s = time.perf_counter() - t_report

        # honest end-to-end wall clock: everything since segmentation began,
        # including model load, frame extraction, inference, and this report pass
        total_wall_s = time.perf_counter() - pipeline_t0
        run_stats = report.aggregate_run_stats(
            per_clip_stats, video_duration_s=duration, segment_s=segment_s,
            clips_ok=len(analyses), clips_failed=clips_failed,
            total_wall_s=total_wall_s,
            timings={"segment_s": segment_s, "extract_s": extract_s,
                     "model_load_s": model_load_s, "report_s": report_s},
            on_device=(backend == "local"),
        )
        session_doc = report.build_session_doc(
            version=__version__, video_name=video.name, video_duration_s=duration,
            model=model_name, backend=backend,
            settings={"fps": args.fps, "max_frames": args.max_frames, "max_side": args.max_side,
                      "segment": args.segment, "kv_bits": args.kv_bits},
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            run_stats=run_stats, facts=facts, clips=clip_records,
        )
        if moments:
            session_doc["moments"] = moments
        if contact_quality:
            session_doc["contact_quality"] = contact_quality
            if args.heatmap:
                try:
                    from .court import build_courtmap
                    anchored = [m for m in moments if m.get("contact_frame") and m.get("contact_px")]
                    if anchored:
                        cm = build_courtmap(
                            out_dir / anchored[0]["contact_frame"],
                            [{"t_s": m["t_s"], "player": m["player"], "code": m["code"],
                              "severity": m["severity"], "xy": m["contact_px"]} for m in anchored],
                            out_dir / "courtmap.json")
                        _log("  court map: " + (f"{len(cm['positions'])} positions mapped"
                                                if cm else "court not detected - skipped"))
                except Exception as e:  # noqa: BLE001 - experimental, never fatal
                    _log(f"  court map failed ({type(e).__name__}) - skipped")
        _finalize_reports(out_dir, session_doc, markdown)
        _log("\n" + report.cost_summary_line(run_stats))
    finally:
        vlm.close()

    total_strokes = sum(len(a.strokes) for a in analyses)
    tail = " (interrupted)" if interrupted else ""
    _log(f"done{tail}: {len(analyses)} clips, {total_strokes} strokes, {clips_failed} skipped -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
