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
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">> Bootstrapping Polaris in namespace '$NS'. Ctrl-C now if that's wrong."
sleep 3

# polaris-postgres (Task 9, revised) creates a dedicated "polaris" DB user
# via the chart's userDatabase field, not the superuser — bootstrap connects
# as that same user (matches what charts/polaris/values.yaml's
# relationalJdbc config uses, so both agree on who owns the schema).
PG_USER=$(kubectl get secret polaris-postgres -n "$NS" -o jsonpath='{.data.POSTGRES_USER_NAME}' | base64 -d)
PG_PASSWORD=$(kubectl get secret polaris-postgres -n "$NS" -o jsonpath='{.data.POSTGRES_USER_PASSWORD}' | base64 -d)
ROOT_CLIENT_SECRET=$(openssl rand -hex 20)

echo ">> Bootstrapping realm POLARIS (root principal: $ROOT_CLIENT_ID)"
kubectl run polaris-bootstrap --rm -it --restart=Never -n "$NS" \
  --image=apache/polaris-admin-tool:1.7.0 \
  --env="POLARIS_PERSISTENCE_TYPE=relational-jdbc" \
  --env="QUARKUS_DATASOURCE_USERNAME=${PG_USER}" \
  --env="QUARKUS_DATASOURCE_PASSWORD=${PG_PASSWORD}" \
  --env="QUARKUS_DATASOURCE_JDBC_URL=jdbc:postgresql://polaris-postgres:5432/polaris" \
  -- bootstrap -r POLARIS -c "POLARIS,${ROOT_CLIENT_ID},${ROOT_CLIENT_SECRET}"

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

pip install --quiet apache-polaris

echo ">> Dry run first — read the output before applying for real:"
polaris --host localhost --port 8181 \
  --client-id "$ROOT_CLIENT_ID" --client-secret "$ROOT_CLIENT_SECRET" \
  setup apply --dry-run "$HERE/polaris-setup-config.yaml"

cat <<'EOF'

If the dry run looks right, apply for real:

  polaris --host localhost --port 8181 \
    --client-id "$ROOT_CLIENT_ID" --client-secret "$ROOT_CLIENT_SECRET" \
    setup apply bootstrap/polaris-setup-config.yaml

Then read its output for the generated "loader" and "lakehouse-ui" principal
credentials (printed once, at creation) and store them:

  kubectl create secret generic loader-polaris-credentials -n lakehouse \
    --from-literal=CLIENT_ID=loader --from-literal=CLIENT_SECRET=<printed>

  kubectl create secret generic lakehouse-ui-polaris-credentials -n lakehouse \
    --from-literal=POLARIS_CLIENT_ID=lakehouse-ui \
    --from-literal=POLARIS_CLIENT_SECRET=<printed>

If the dry run instead reports an invalid field in polaris-setup-config.yaml
(likely the S3 storage block — endpoint/region/path_style_access), run
`polaris catalogs create --help` to see the real flag names, fix the YAML,
and dry-run again before applying.
EOF
