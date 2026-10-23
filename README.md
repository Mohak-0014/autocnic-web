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
`AUTOCNIC_BASIC_AUTH` (`user:password`) in the dashboard. The blueprint uses
Render's **free plan**: the service sleeps after ~15 minutes idle (the next visit
takes about a minute) and stored sessions/certificates reset on each restart. Open
the site shortly before a demo to wake it. For persistence, see the comment in
`render.yaml`.

Analyzing a repo **runs that repo's Python code on the server**, so:

- **No password set** → the server runs the **bundled demo projects only**. It's
  safe to leave open, but visitors can't submit their own repos.
- **Password set** → everyone is asked to log in, and logged-in users can analyze any
  public GitHub/GitLab/Bitbucket repo. Each project runs in its own process with
  a timeout and memory limit, so a broken repo can't take the server down.

## For the invigilator

Open the deployment URL (and log in with the credentials you were given, if
asked). Then:

1. **Analyze**: click one of the bundled examples under the input box.
   `shopfront` is the best start. Discovery takes a few seconds.
2. **Inputs**: change one or more input values and apply them.
3. **Live tracker**: see which nodes were recomputed (green) and which were
   skipped with a certificate, step through the timeline.
4. **Certificates**: open any certificate to see the plain-English statement,
   the independent checker's verdict (ACCEPT) and the JSON.

To try your own project, download the starter template from the Guide page,
push it to a public GitHub repo and paste its URL (needs the password-protected
mode above). If a project hangs or crashes, you get an error message and can
carry on; nothing else is affected.

## Making your repo work with the app

Your repo needs an `autocnic_pipeline.py` that declares the inputs and steps.
Open **/guide** in the running app for a step-by-step walkthrough, or download
the starter project from **/api/template.zip** (source in
[`autocnic_web/template/`](autocnic_web/template/)).
