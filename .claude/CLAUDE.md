# Repo facts for Claude

This is an Argo CD GitOps repo managed with `argocd-gitops-plugin`. Read the
plugin skill `argocd-repo-conventions` before generating or editing any file here.

- GitOps repo URL: https://github.com/OmalCooray/test-k8s-configs
- Default branch: master
- Argo CD namespace: argocd
- Environments: local
- Destination cluster for local: https://kubernetes.default.svc

## Catalog inventory

_(Updated by `/argocd-add-chart`.)_

| App | Upstream chart | Version | Repo |
|-----|----------------|---------|------|
| airflow | airflow | 1.22.0 | https://airflow.apache.org |

## Deployment matrix

_(Updated by `/argocd-deploy`.)_

| App | local |
|-----|----------------|
| airflow | yes |
