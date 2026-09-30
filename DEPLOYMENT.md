# Deploying Jeff to Kubernetes

Jeff is a decision inference server built on Qwen3.5. This document covers
everything needed to deploy the container image to a Kubernetes cluster on
EKS with S3-hosted checkpoints.

## Prerequisites

- An EKS cluster with an OIDC provider (for IRSA)
- The EFS CSI driver is **not** required — we use S3 + emptyDir
- `kubectl` configured for your cluster
- `aws` CLI with permissions to create S3 buckets and IAM roles
- The checkpoint files (a `checkpoints/jeff-0.8b/` directory containing
  `model.safetensors`, `readout.safetensors`, `decision_config.json`,
  `tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`,
  `processor_config.json`, `config.json`, and `LICENSE`)

## Architecture

```
S3 bucket ──► initContainer (aws-cli) ──► emptyDir ──► jeff container (read-only)
                                         (ephemeral)
```

On each pod start, the initContainer downloads the 1.7 GB checkpoint
from S3 to an ephemeral `emptyDir`. The main container then mounts it
read-only and serves inference. No PVC or EBS volume — pods are fully
stateless and can land on any node in any AZ.

The download adds ~30s to startup. If this becomes a problem, switch to
EFS (RWX mount, no download) or a PVC (download once, cached) — see
"Alternative storage strategies" below.

## Image

The Docker image is published to `ghcr.io/winjer/jeff` by a GitHub Action
on every push to `cpu-fallback` and on version tags (`v*`).

```
ghcr.io/winjer/jeff:cpu-fallback       # branch tag
ghcr.io/winjer/jeff:sha-<commit>       # commit SHA tag
ghcr.io/winjer/jeff:1.0.0             # semver tag (on git tag v1.0.0)
```

The image is ~282 MB compressed. It contains only the server dependencies
(no training/research tooling) and uses a CPU-only PyTorch wheel (no CUDA
libraries), keeping it small. Checkpoints are **not** in the image.

## Step 1: Upload the checkpoint to S3

```bash
aws s3 mb s3://jeff-checkpoints          # or use an existing bucket
aws s3 cp --recursive checkpoints/jeff-0.8b/ s3://jeff-checkpoints/jeff-0.8b/
```

## Step 2: Create the IAM role for IRSA

Create an IAM role with a trust policy for your cluster's OIDC provider
and an inline policy granting read access to the S3 bucket.

Trust policy (replace `OIDC_PROVIDER` and `ACCOUNT_ID`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/OIDC_PROVIDER"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "OIDC_PROVIDER:sub": "system:serviceaccount:default:jeff"
        }
      }
    }
  ]
}
```

S3 read policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::jeff-checkpoints"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::jeff-checkpoints/*"
    }
  ]
}
```

Note the role ARN — you'll set it in the ServiceAccount annotation in the
manifest.

## Step 3: Configure the manifest

Edit `k8s.yaml` and replace:

1. **S3 bucket name** — in the initContainer env var `S3_BUCKET` (default
   `jeff-checkpoints`):
   ```yaml
   env:
     - name: S3_BUCKET
       value: jeff-checkpoints          # ← your bucket name
   ```

2. **IAM role ARN** — in the ServiceAccount annotation (default
   `arn:aws:iam::ACCOUNT_ID:role/jeff-s3-read`):
   ```yaml
   metadata:
     annotations:
       eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/jeff-s3-read
   ```

3. **Image tag** — in the container spec (default
   `ghcr.io/winjer/jeff:cpu-fallback`):
   ```yaml
   image: ghcr.io/winjer/jeff:cpu-fallback
   ```

4. **Namespace** — the manifest assumes `default`. To deploy elsewhere,
   add `namespace:` to each resource or use `kubectl -n`.

5. **API key** — optional. If you want to require an API key for
   inference, create a Kubernetes Secret and uncomment the
   `JEFF_API_KEY` env var:
   ```bash
   kubectl create secret generic jeff-api-key --from-literal=key=your-secret-key
   ```
   The server checks the `Authorization: Bearer <key>` header.

## Step 4: Deploy

```bash
kubectl apply -f k8s.yaml
```

Watch the initContainer download the checkpoint, then the main container
start:

```bash
kubectl logs -f deployment/jeff -c download-checkpoint
kubectl logs -f deployment/jeff -c jeff
```

Verify the service is up:

```bash
kubectl port-forward svc/jeff 8000:8000
curl -s localhost:8000/health
```

## Resource requirements

| Setting | Value | Notes |
|---------|-------|-------|
| Memory request | 5 Gi | Model weights load as fp32 on CPU (3.2 GB) + runtime overhead (~1.2 GB) = ~4.4 GB RSS |
| Memory limit | 6 Gi | Inference doesn't grow memory significantly |
| CPU request | 2 | For reasonable inference latency |
| emptyDir size | 5 Gi | Holds the 1.7 GB checkpoint |

The model weights are bfloat16 on disk (1.6 GB) but loaded as float32 on
CPU (see `src/jeff/model.py:177`), doubling the in-memory size. If you
change that line to use `torch.bfloat16` on CPU, memory drops to ~3 GB
and the requests/limits can be halved.

## Storage strategies

The current manifest uses **S3 + emptyDir**: the simplest option that
requires no persistent volume. Trade-offs:

| Strategy | Node-pinned | Multi-AZ | Startup | Stores to manage |
|----------|:-----------:|:--------:|---------|:----------------:|
| **S3 + emptyDir (current)** | No | Yes | ~30s | S3 only |
| EFS mount | No | Yes | ~10s | EFS only |
| EBS PVC + S3 initContainer | Yes | No | Instant (after first boot) | S3 + PVC |
| Bake into image | No | Yes | Instant | Image only |

To switch to EFS:
1. Create an EFS filesystem and access point
2. Install the EFS CSI driver on the cluster
3. Replace the `emptyDir` volume with a `persistentVolumeClaim` using a
   `ReadWriteMany` StorageClass backed by EFS
4. Remove the initContainer (pre-upload the checkpoint to EFS once)

To switch to EBS PVC:
1. Change the volume from `emptyDir` to a `persistentVolumeClaim` with
   `ReadWriteOnce` and a standard EBS StorageClass
2. Keep the initContainer — it will download on first boot only
3. Be aware: the pod is now pinned to one node and one AZ

## API

The server exposes a single inference endpoint:

```
POST /v1/systemone
```

Example request:

```bash
curl -s localhost:8000/v1/systemone \
  -H 'content-type: application/json' \
  -d '{
    "model": "jeff-latest",
    "state": "Refund request: the customer says the parcel arrived crushed and wants their money back.",
    "questions": {
      "route": {"type": "choice", "instructions": "Which team should handle this?",
                "criteria": {"1": "Refunds and payments", "2": "Damaged or lost parcels", "3": "Account and login problems"}},
      "angry": {"type": "noul", "instructions": "Is the customer angry?"}
    }
  }'
```

Other endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check — returns model status, checkpoint path, max options |
| `/v1/models` | GET | List available models (requires API key if set) |
| `/` | GET | HTML playground UI |

Question types:
- `choice` — multi-class classification with named options (`criteria` dict)
- `score` — ordinal scoring with ordered criteria (list)
- `noul` — probability of "true" on a single criterion (binary)

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JEFF_CHECKPOINT` | `/app/checkpoints/jeff-0.8b` | Path to the checkpoint directory |
| `JEFF_HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Listen port |
| `JEFF_API_KEY` | (unset) | If set, requires `Authorization: Bearer <key>` header |
| `JEFF_BACKEND` | `pytorch` | `pytorch` or `mlx` (Apple silicon only) |
| `JEFF_DEVICE` | (auto) | `cpu`, `cuda`, or `mps` — explicit device selection |
| `S3_BUCKET` | `jeff-checkpoints` | S3 bucket for checkpoint download (initContainer only) |

## Troubleshooting

**initContainer fails with `AccessDenied`**: The IRSA role isn't attached
or doesn't have S3 read permissions. Check:
```bash
kubectl describe pod -l app=jeff
```

**initContainer fails with `NoSuchBucket`**: Update the `S3_BUCKET` env
var to match your bucket name.

**Main container crashes with `FileNotFoundError: decision_config.json`**:
The checkpoint didn't download correctly. Check the initContainer logs:
```bash
kubectl logs -l app=jeff -c download-checkpoint
```

**Main container OOM-killed**: Increase the memory limit. The model uses
~4.4 GB RSS. The 6 Gi limit should be enough, but if the node is under
memory pressure, k8s may kill it.

**500 on inference (Triton `0 active drivers`)**: This was fixed by
`src/jeff/_cpu_fallback.py`, which blocks the `fla` import on CPU-only
machines so transformers uses its pure-PyTorch reference path. If you see
this error, ensure `jeff._cpu_fallback.apply_cpu_fallback()` is called in
`jeff/__init__.py` before transformers is imported.
