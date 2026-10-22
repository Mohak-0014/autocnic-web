# AutoCNIC web

Paste a GitHub repo and AutoCNIC finds which of its dependencies only matter
under some condition. When an input changes, it recomputes only what the change
can actually affect, and it issues an independently verified certificate for
every value it skips.

Full documentation: [autocnic_web/README.md](autocnic_web/README.md)

## Run locally

```bash
cd autocnic_web/backend
pip install -r requirements.txt
python main.py            # http://localhost:8000
```

## Deploy

**Docker** (any host):

```bash
docker build -t autocnic-web .
docker run -p 8000:8000 -v autocnic-data:/data -e AUTOCNIC_BASIC_AUTH=me:secret autocnic-web
```

**Render**: New → Blueprint → select this repo (`render.yaml`). Set
`AUTOCNIC_BASIC_AUTH` in the dashboard.

> ⚠️ Analyzing a repo **runs that repo's Python code on the server**. On a public
> deployment, set `AUTOCNIC_BASIC_AUTH` so only people you trust can submit repos.

## Making your repo work with the app

Your repo needs an `autocnic_pipeline.py` that declares the inputs and steps.
Open **/guide** in the running app for a step-by-step walkthrough, or download
the starter project from **/api/template.zip** (source in
[`autocnic_web/template/`](autocnic_web/template/)).
