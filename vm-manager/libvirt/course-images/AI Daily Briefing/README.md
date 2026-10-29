# AI Daily Briefing course image

This directory is a reviewed build bundle. `AI Daily Briefing.md` is source guidance only; the image builder never executes Markdown. It executes the reviewed `provision.sh`, which installs bounded read-only checkers.

## What the demonstration detects

- P01-P06: deterministic guest probes for Docker, the project directory, volume, normalized Compose contract, running container, and n8n readiness.
- P07: reports `failed` when the editor is unreachable and `unknown` when HTTP works but owner setup still needs platform/browser confirmation.
- P08-P20 and P22-P23: report `unknown` until a platform-owned read-only n8n workflow/execution observer is implemented.
- P21 and P24: require both n8n execution evidence and controlled inbox evidence or explicit learner confirmation.

The learner currently has passwordless sudo, so guest checkers support teaching progress but are not an anti-cheat security boundary.

## When the scripts enter the VM

They are installed while the **course golden image** is built, not each time a learner VM starts:

1. The host creates a reproducible archive of this directory and embeds it in cloud-init as `/var/tmp/aivirteach-course-image.tar.gz`.
2. The builder VM verifies the archive SHA-256 and extracts it to `/opt/aivirteach/course-image`.
3. `provision.sh` installs the single-step and batch dispatchers at `/usr/local/bin/aivirteach-check-step` and `/usr/local/bin/aivirteach-check-progress`, the root-owned checkers under `/usr/local/lib/aivirteach/checks/ai-daily-briefing-v2/`, and the manifest/source under `/opt/aivirteach/courses/ai-daily-briefing-v2/`.
4. Only after provisioning and cleanup succeed does the builder write the image/course markers. The host verifies those markers from the powered-off disk before publishing the qcow2 image.

Every learner overlay created from that golden image therefore inherits the same immutable starting copy of the detection scripts.

## Validate without building an image

```bash
./tests/test-checks.sh
../../scripts/build-base-image.sh \
  --validate-only \
  --course-image-dir "$PWD"
```

The second command non-executably validates the manifest allowlist, bundle paths, file/size limits, shell syntax, reproducible archive, and SHA-256. It does not use sudo, download Ubuntu, or start a VM.

## Build the course golden image

Run from `vm-manager/libvirt`:

```bash
sudo ./scripts/build-base-image.sh \
  --course-image-dir "$PWD/course-images/AI Daily Briefing"
```

The manifest explicitly selects Ubuntu 22.04 and writes:

```text
/var/lib/libvirt/images/aivirteach/base/ai-daily-briefing-v2-ubuntu-22.04-v1.qcow2
```

The host publishes the image only when both completion markers match the expected image name, course ID, and exact course-bundle SHA-256 in the powered-off builder disk.

For a manual end-to-end demonstration, create a learner VM explicitly from that image:

```bash
sudo ./scripts/create-learner-vm.sh lab-ai-daily-demo \
  --base-image /var/lib/libvirt/images/aivirteach/base/ai-daily-briefing-v2-ubuntu-22.04-v1.qcow2 \
  --os-release 22.04
```

## Run a checker inside a learner VM

```bash
sudo /usr/local/bin/aivirteach-check-step P01
sudo /usr/local/bin/aivirteach-check-step P06
sudo /usr/local/bin/aivirteach-check-step P08  # intentionally returns unknown
sudo /usr/local/bin/aivirteach-check-progress P01 P02 P03
```

Exit status 0 means `passed`, 1 means `failed`, and 2 means `unknown` or unavailable evidence. Output is bounded JSON and must never contain credentials, article bodies, or email HTML.

The Markdown references `AI_Daily_Briefing_Gemini_Code_No_Comments.txt`, but that attachment is not present yet. Validation therefore reports `bundle_status=incomplete`; add it before treating this as a production-ready course image or expecting the Code-node target to be reproducible/hash-verifiable. P01-P07 remain usable for this demonstration.

The current `POST /v1/vms` API does not yet accept a trusted `course_id`, so it will not automatically choose this image. The next orchestration step is a server-owned `course_id -> allowlisted golden image` mapping; never accept an arbitrary image path from the browser. Progress intentionally keeps two IDs: Server/Agent use canonical `ai-daily-briefing`, while the course JSON's reviewed `progressPolicy` maps it to this immutable runtime bundle ID, `ai-daily-briefing-v2`.
