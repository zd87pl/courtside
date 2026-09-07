# Third-party code, models, and sample data

The root `LICENSE` covers this repository's own code. Dependencies, model weights,
provider services, and footage retain their own licenses and terms.

The default API image installs **Ultralytics** and bundles `yolo11n-pose.pt`.
Ultralytics describes AGPL-3.0 and Enterprise licensing options for its code and
models, including proprietary and SaaS deployments. The receiving team must
choose an appropriate licensing path or replace/remove that component before
shipping a product under its intended terms. Merely sending `pose:false` in a
request does not remove bundled software from the image.
[Ultralytics licensing](https://www.ultralytics.com/license).

Other components include Python packages listed in `api/constraints.txt`, ffmpeg
and OS packages in `api/Dockerfile`, browser dependencies in `cloud/package-lock.json`,
and local MinIO images in `api/docker-compose.yml`. Retain applicable notices and
review the exact versions distributed. MinIO is used as a development stand-in;
production examples accept a private S3-compatible service of your choice.

OpenRouter is a service, not a model license. Verify the terms, supported inputs,
availability, and data handling of the selected downstream model/provider in your
own account. Local Hugging Face models likewise have their own model-card terms.

Do not assume sample images/reports or locally downloaded tennis videos are cleared
for redistribution or model processing. Raw footage and local weights are excluded
from source packaging. Review the included `examples/` assets with their owner
before sharing them outside the intended handoff.
