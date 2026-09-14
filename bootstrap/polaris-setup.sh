#!/usr/bin/env bash
# One-time-per-realm setup for the Polaris catalog on this cluster:
#   1. Bootstrap the POLARIS realm + root principal (apache/polaris-admin-tool).
#   2. Apply polaris-setup-config.yaml (catalog/namespace/principals/grants)
#      via the `polaris setup apply` CLI.
#
# Not Argo CD-managed — see the header comment in polaris-setup-config.yaml.
# Run by hand after `minio`, `polaris-postgres`, and `polaris` are all
# Synced/Healthy. See docs/superpowers/specs/2026-09-11-iceberg-lakehouse-design.md.
set -euo pipefail
NS=lakehouse
ROOT_CLIENT_ID=root
POLARIS_ADMIN_TOOL_IMAGE="apache/polaris-admin-tool:1.7.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONTEXT="$(kubectl config current-context)"
echo ">> Using kube-context: ${CONTEXT}"
kubectl cluster-info >/dev/null

echo ">> Bootstrapping Polaris in namespace '$NS' on context '$CONTEXT'. Ctrl-C now if that's wrong."
sleep 3

# polaris-postgres (Task 9, revised) creates a dedicated "polaris" DB user
# via the chart's userDatabase field, not the superuser — bootstrap connects
# as that same user (matches what charts/polaris/values.yaml's
# relationalJdbc config uses, so both agree on who owns the schema).
PG_USER=$(kubectl get secret polaris-postgres -n "$NS" -o jsonpath='{.data.POSTGRES_USER_NAME}' | base64 -d)
PG_PASSWORD=$(kubectl get secret polaris-postgres -n "$NS" -o jsonpath='{.data.POSTGRES_USER_PASSWORD}' | base64 -d)
if [ -z "$PG_USER" ] || [ -z "$PG_PASSWORD" ]; then
  echo "FAIL: could not read POSTGRES_USER_NAME/POSTGRES_USER_PASSWORD from Secret polaris-postgres in namespace $NS." >&2
  exit 1
fi
ROOT_CLIENT_SECRET=$(openssl rand -hex 20)

echo ">> Bootstrapping realm POLARIS (root principal: $ROOT_CLIENT_ID)"
# NOTE: the root client secret is passed as a container arg below, so it is
# briefly visible via `kubectl get pod -o yaml` and the API server audit log
# while this pod exists (a few seconds, until we delete it below). Accepted
# as a reasonable tradeoff for a one-shot runbook — do not reuse this
# pattern for anything longer-lived.
#
# --rm is intentionally omitted here (unlike a plain interactive `kubectl
# run --rm -it`) so we can read the pod's actual terminated exit code before
# it disappears — `kubectl run`'s own process exit status isn't a reliable
# signal of whether the bootstrap succeeded. We delete the pod ourselves
# once we've captured that.
kubectl run polaris-bootstrap --restart=Never -n "$NS" \
  --image="$POLARIS_ADMIN_TOOL_IMAGE" \
  --env="POLARIS_PERSISTENCE_TYPE=relational-jdbc" \
  --env="QUARKUS_DATASOURCE_USERNAME=${PG_USER}" \
  --env="QUARKUS_DATASOURCE_PASSWORD=${PG_PASSWORD}" \
  --env="QUARKUS_DATASOURCE_JDBC_URL=jdbc:postgresql://polaris-postgres:5432/polaris" \
  -- bootstrap -r POLARIS -c "POLARIS,${ROOT_CLIENT_ID},${ROOT_CLIENT_SECRET}"

echo ">> Waiting for polaris-bootstrap pod to finish..."
BOOTSTRAP_EXIT=""
for _ in $(seq 1 60); do
  PHASE=$(kubectl get pod polaris-bootstrap -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  if [ "$PHASE" = "Succeeded" ] || [ "$PHASE" = "Failed" ]; then
    BOOTSTRAP_EXIT=$(kubectl get pod polaris-bootstrap -n "$NS" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || true)
    break
  fi
  sleep 1
done

echo ">> polaris-bootstrap pod logs:"
kubectl logs polaris-bootstrap -n "$NS" || true
kubectl delete pod polaris-bootstrap -n "$NS" --ignore-not-found

if [ -z "$BOOTSTRAP_EXIT" ] || [ "$BOOTSTRAP_EXIT" != "0" ]; then
  echo "FAIL: bootstrap pod exited with code '${BOOTSTRAP_EXIT:-unknown (timed out waiting for pod to finish)}' — see logs above. No credentials Secret created." >&2
  exit 1
fi

kubectl create secret generic polaris-root-credentials -n "$NS" \
  --from-literal=CLIENT_ID="$ROOT_CLIENT_ID" \
  --from-literal=CLIENT_SECRET="$ROOT_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
echo ">> Root credentials stored in Secret polaris-root-credentials (ns $NS)."

echo ">> Port-forwarding polaris:8181 to apply the catalog/principal/grants config..."
kubectl port-forward -n "$NS" svc/polaris 8181:8181 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT
sleep 3

# set -e doesn't apply to background jobs, so confirm the forward is both
# still running and actually accepting connections before relying on it —
# otherwise a dead/refused port-forward surfaces later as a confusing
# connection-refused error from `polaris`/curl instead of a clear message.
if ! kill -0 "$PF_PID" 2>/dev/null; then
  echo "FAIL: kubectl port-forward exited immediately — check svc/polaris exists and is reachable." >&2
  exit 1
fi
if ! bash -c 'exec 3<>/dev/tcp/localhost/8181' 2>/dev/null; then
  echo "FAIL: port 8181 not accepting connections after port-forward — see any kubectl port-forward output above." >&2
  exit 1
fi

pip install --quiet "apache-polaris==1.7.0"

echo ">> Dry run first — read the output before applying for real:"
polaris --host localhost --port 8181 \
  --client-id "$ROOT_CLIENT_ID" --client-secret "$ROOT_CLIENT_SECRET" \
  setup apply --dry-run "$HERE/polaris-setup-config.yaml"

cat <<'EOF'

If the dry run looks right, apply for real. $ROOT_CLIENT_SECRET only exists
in this script's own process, not your shell, so fetch it back from the
Secret this script just created:

  ROOT_CLIENT_SECRET=$(kubectl get secret polaris-root-credentials -n lakehouse -o jsonpath='{.data.CLIENT_SECRET}' | base64 -d)
  polaris --host localhost --port 8181 \
    --client-id root --client-secret "$ROOT_CLIENT_SECRET" \
    setup apply bootstrap/polaris-setup-config.yaml

Then read its output: two JSON lines, one per principal (in the same order
as `principals:` in polaris-setup-config.yaml — loader first, lakehouse-ui
second), each `{"clientId": "...", "clientSecret": "..."}`.

IMPORTANT: unlike `root` (whose client_id really is the literal string
"root", because the admin-tool's `bootstrap` command sets it explicitly),
`loader` and `lakehouse-ui` here are principal *names*, not OAuth
client_ids — `setup apply`'s principal-creation flow auto-generates an
opaque clientId for each one. Use that printed clientId as CLIENT_ID below,
NOT the principal's name — `client_id=loader` gets a real, silent
"unauthorized_client" from Polaris's OAuth endpoint (confirmed live:
2026-09-14, first Definition-of-Done rerun after a cluster reset). Store the
printed clientId/clientSecret pair as-is:

  kubectl create secret generic loader-polaris-credentials -n lakehouse \
    --from-literal=CLIENT_ID=<printed clientId for loader> \
    --from-literal=CLIENT_SECRET=<printed clientSecret for loader>

  kubectl create secret generic lakehouse-ui-polaris-credentials -n lakehouse \
    --from-literal=POLARIS_CLIENT_ID=<printed clientId for lakehouse-ui> \
    --from-literal=POLARIS_CLIENT_SECRET=<printed clientSecret for lakehouse-ui>

If the dry run instead reports an invalid field in polaris-setup-config.yaml
(likely the S3 storage block — endpoint/region/path_style_access), run
`polaris catalogs create --help` to see the real flag names, fix the YAML,
and dry-run again before applying.
EOF
