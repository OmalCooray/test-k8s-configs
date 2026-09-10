# test-k8s-configs

Argo CD GitOps configuration, managed with the
[`argocd-gitops-plugin`](https://github.com/OmalCooray/argocd-gitops-plugin).

> Requires **Argo CD ≥ 2.6** — the `Application` manifests use multi-source apps (`$values`).

## Layout

| Path | Purpose |
|------|---------|
| `charts/<app>/` | Catalog: a Helm wrapper chart per app (`Chart.yaml` pins the upstream chart, `values.yaml` holds base overrides). Nothing here deploys on its own. |
| `environments/<env>/apps/<app>.yaml` | An Argo CD `Application` — the fact that `<app>` is deployed to `<env>`. |
| `environments/<env>/values/<app>.yaml` | Optional per-environment value overlay. |
| `environments/<env>/root.yaml` | The root app-of-apps for `<env>`; syncs everything in that `apps/` folder. |
| `bootstrap/install.sh` | One-time Argo CD install for a fresh cluster. |

## First-time setup

```bash
./bootstrap/install.sh local
```

## Day-to-day (via the plugin)

- Add a chart to the catalog: `/argocd-add-chart <name> [version]`
- Deploy it to an environment: `/argocd-deploy <app> <env>`
- Check for drift: `/argocd-audit [env]`
