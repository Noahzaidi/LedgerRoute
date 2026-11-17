# Source release and GitHub publication

The source packaging command creates `output/ledgerroute-source.zip` containing a `ledgerroute/` directory with the maintained application, synthetic data, tests, and documentation. It excludes Git metadata, environment files, local databases, caches, and generated artifacts.

```bash
python scripts/package_source.py
```

To publish from a source archive with fresh Git history, extract it to a new directory and open a terminal inside its `ledgerroute/` folder:

```bash
git init -b main
git add .
git diff --cached --stat
git commit -m "feat: introduce LedgerRoute invoice resolution service"
gh repo create ledgerroute --public --source . --remote origin --push --description "Auditable invoice classification and supplier resolution for finance workflows."
```

The last command publishes to the account authenticated in GitHub CLI. Check `gh auth status` first. Alternatively, create an empty `ledgerroute` repository in the GitHub UI, then add its URL as `origin` and push `main` from the extracted directory.

The README and package metadata use `Noahzaidi/ledgerroute`; update those links if publishing under a different account or repository name. Suggested repository topics: `invoice-processing`, `accounts-payable`, `supplier-resolution`, `fastapi`, `python`, and `llm`.

After publication, enable private vulnerability reporting in repository settings and confirm that the checks workflow passes. Before tagging a release, run the [development checks](../README.md#development), the offline sample pipeline, and a deployment smoke check in the target environment.

The application currently supports running from a checkout or container. A standalone wheel with bundled reference data and a managed schema upgrade path is not part of this release process.
