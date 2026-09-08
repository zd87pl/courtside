# Third-party code, models, and sample data

The root `LICENSE` covers this repository's own code. Dependencies, model weights,
provider services, and footage retain their own licenses and terms.

The default API image **omits Ultralytics, Torch, and YOLO pose weights**. Pose is
disabled in API options and requires an explicit build/runtime opt-in described
in [Operations](../api/OPERATIONS.md#optional-pose-image). That optional image
installs Ultralytics and bundles `yolo11n-pose.pt`. Ultralytics describes AGPL-3.0
and Enterprise options for its code and models; the receiving team must choose
an appropriate licensing path for its intended deployment before enabling it.
Sending `pose:false` does not remove components from an opted-in image.
[Ultralytics licensing](https://www.ultralytics.com/license).

Other components include Python packages listed in `api/constraints.txt`, ffmpeg
and OS packages in `api/Dockerfile`, browser dependencies in `cloud/package-lock.json`,
and local MinIO images in `api/docker-compose.yml`. Retain applicable notices and
review the exact versions distributed. MinIO is used as a development stand-in;
production examples accept a private S3-compatible service of your choice.

OpenRouter is a service, not a model license. Verify the terms, supported inputs,
availability, and data handling of the selected downstream model/provider in your
own account. Local Hugging Face models likewise have their own model-card terms.

The included demo documents its [synthetic footage and hand-authored analyses](../examples/README.md#provenance-read-this); it is not an accuracy benchmark.
Inventory sample assets separately when distributing them. User-supplied or locally
downloaded footage still requires appropriate permission, consent, and retention
choices. Raw footage and local weights are excluded from source packaging. Removing
the pose dependency does not settle the terms of other dependencies or services.
