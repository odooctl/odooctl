# GitOps examples

`github-actions-pr.yml` renders PR overlays without a cluster credential,
passes them between jobs as an artifact, and publishes with a short-lived
GitHub App installation token. Configure the `preview` GitHub Environment with
required reviewers and store the App ID/private key there.

`argocd-applicationset.yaml` watches the dedicated `gitops-previews` branch.
Configure repository access in Argo CD with a least-privilege read-only GitHub
App credential or another short-lived credential integration. Do not commit
repository or cluster credentials beside the ApplicationSet.

Replace repository URLs, the Argo CD project, initializer image, and domains
before use. Limit the Argo CD project to namespaces matching the odooctl
project prefix and to the resource kinds emitted by the renderer.
