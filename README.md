# dantetemplar.github.io → cv.belkov.su

The CV website of Ruslan Belkov. It is generated from a Google Doc and published on GitHub Pages.

## How it works

The `.github/workflows/deploy.yml` workflow runs every Monday, on every push to `main`, and from the **Actions → Build & deploy CV → Run workflow** button.

1. `python scripts/cv.py fetch` downloads the Google Doc as HTML (`export?format=zip`) into `source/`.
2. `python scripts/cv.py build` turns the export into structured content `content/cv.json` plus `content/photo.jpg`, then builds `_site/`. The site contains:
   - `index.html`: semantic HTML with meta description, Open Graph, JSON-LD (`ProfilePage` / `Person`) and a dark theme;
   - `robots.txt`;
   - `sitemap.xml`;
   - `CNAME`.
3. If the CV text changed, the workflow commits `content/`. This gives a history of CV edits.
4. `_site/` is deployed to GitHub Pages.

## What lives where

| What | Where |
|---|---|
| CV text (summary, experience, hackathons…) | Google Doc. Use **Heading 1** for sections and **Heading 2** for entries |
| Name, job title, contacts, SEO description | `site.toml`, because the Doc's HTML export does not include the page header |
| Doc ID | `google_doc_id` in `site.toml` (the `GOOGLE_DOC_ID` repository variable overrides it) |

The doc must be shared as **"Anyone with the link → Viewer"**.

Formatting conventions in the doc that the parser understands:
- `Title | Jul 2024 - Present` shows the date on the right.
- `Title — 2025 Winner` shows a result badge.
- A line starting with `🔧 FastAPI, PostgreSQL` becomes tech-stack tags.
- A paragraph containing only links becomes a row of links.

## Local build

```bash
pip install -r requirements.txt
GOOGLE_DOC_ID=<id> python scripts/cv.py fetch
python scripts/cv.py build
python -m http.server -d _site
```

## Note

GitHub pauses scheduled workflows in a repository after 60 days without commits. As long as the CV changes now and then, the sync commits keep it alive. If it gets paused, GitHub sends an email; enable it again in the Actions tab.
