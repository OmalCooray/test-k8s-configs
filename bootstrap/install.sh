#!/usr/bin/env bash
# Bootstrap Argo CD onto the current kube-context, then hand control to Git.
#
# Usage:  ./bootstrap/install.sh [kube-context]
#
# Idempotent: safe to re-run. After the first run, Argo CD manages everything
# else (including itself if you add an app for it) from https://github.com/OmalCooray/test-k8s-configs.
set -euo pipefail

ARGOCD_NAMESPACE="argocd"
ARGOCD_CHART_VERSION="10.8.4"
ENV_NAME="local"
CONTEXT="${1:-$(kubectl config current-context)}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ">> Using kube-context: ${CONTEXT}"
kubectl --context "${CONTEXT}" cluster-info >/dev/null

echo ">> Installing/upgrading Argo CD (chart ${ARGOCD_CHART_VERSION}) into ${ARGOCD_NAMESPACE}"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null 2>&1 || helm repo update >/dev/null
helm --kube-context "${CONTEXT}" upgrade --install argo-cd argo/argo-cd \
  --namespace "${ARGOCD_NAMESPACE}" --create-namespace \
  --version "${ARGOCD_CHART_VERSION}" \
  --timeout 10m \
  --wait

echo ">> Waiting for Argo CD CRDs to register"
kubectl --context "${CONTEXT}" wait --for=condition=Established \
  crd/applications.argoproj.io crd/appprojects.argoproj.io --timeout=120s

echo ">> Applying the root application for environment '${ENV_NAME}'"
kubectl --context "${CONTEXT}" apply -n "${ARGOCD_NAMESPACE}" \
  -f "${REPO_ROOT}/environments/${ENV_NAME}/root.yaml"

echo ">> Done. Watch sync status with:  argocd app list   (or the Argo CD UI)"
