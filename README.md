<table>
<tr>
<td width="160" align="center">

<img src="app/resources/prism_logo.png" alt="PRISM logo" width="140"/>

</td>

<td>

# PRISM
### Prompt Reliability & Inconsistency Scoring for Models

*A cross-platform desktop application for benchmarking the behavioral consistency of local LLMs running on Ollama.*

</td>
</tr>
</table>

---

<div align="center">

[Features](#-features) • [Installation](#-installation) • [Configuration](#-configuration) • [Usage](#-usage) • [Architecture](#-project-structure) • [Contributing](#-contributing) • [License](#-license)

</div>

## About

Most LLM benchmarks score a model on a single prompt per question — but real-world usage rarely looks like that. **PRISM** instead measures how *reliably* a model answers when the same question is asked in several semantically equivalent ways.

It runs a controlled evaluation pipeline against local **Ollama** models using validated datasets (ARC‑Challenge, SciQ), scores consistency and prompt sensitivity across five prompt variants (P₀–P₄), and presents the results in a native desktop analytics dashboard — no cloud inference, no API keys required.

---

## Features

- **Local Ollama Inference** — auto-detects installed models, monitors server health, and supports live model downloads with progress tracking.
- **Controlled Benchmark Pipeline** — deterministic evaluation on ARC‑Challenge and SciQ using standardized generation settings.
- **Prompt Reliability Scoring** — consistency, prompt sensitivity, deviation, and format adherence across prompt variants P₀–P₄.
- **Interactive Analytics Dashboard** — native PySide6 UI with KPI cards, charts, per-question drill-down, and side-by-side model comparison.
- **Crash-safe Benchmarking** — auto-saves progress on model crashes, OOM errors, or Ollama failures, with resume support.
- **PDF Report Generation** — export publication-ready benchmark reports in one click.
- **Offline Ready** — ships with bundled results for four baseline models so you can explore the dashboard with zero setup.
- **Community Results Hub** — browse verified public results synced from the [official results repository](https://github.com/Nabin-16/Reliability-test-result-model-versions).

---

## System Requirements

| Requirement | Details |
|---|---|
| OS | Windows 10+, macOS 12+, or a modern Linux distro |
| Python | 3.11 or newer (only needed to run from source) |
| Ollama | Installed and running — [ollama.com](https://ollama.com) |
| Disk | Varies with the local models you pull via Ollama |

---

## Installation

### Option 1 — Download a prebuilt installer (recommended for most users)

Grab the latest release for your platform from the **[GitHub Releases page](https://github.com/karkikaruna/prism/releases)**:

| Platform | Package |
|---|---|
| Windows | `PRISM-Setup-x.x.x.exe` |
| macOS | `PRISM-x.x.x.dmg` |
| Linux | `PRISM-x86_64.AppImage` or `.deb` |

Run the installer, launch PRISM, and make sure Ollama is running in the background.

### Option 2 — Run from source

```bash
# 1. Clone the repository
git clone https://github.com/karkikaruna/prism.git
cd prism

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch the app
python main.py
```

> **Tip:** Make sure Ollama is installed and running (`ollama serve`) before launching PRISM, and that you've pulled at least one model, e.g. `ollama pull llama3`.

---

## Configuration

Copy the example environment file and edit as needed:

```bash
cp .env.example .env
```

| Variable | Description | Required |
|---|---|---|
| `OLLAMA_BASE_URL` | Ollama endpoint (default: `http://localhost:11434`) | No |
| `PRISM_SUPABASE_URL` | Optional Supabase project URL for cloud sync | No |
| `PRISM_SUPABASE_PUBLISHABLE_KEY` | Optional publishable/anon key for syncing results | No |

> ⚠️ **Security note:** Only ever use a **publishable/anon** Supabase key here. Never commit or bundle a service-role or secret key inside the desktop app or the Git repository. Cloud sync is entirely optional — PRISM works fully offline without it.

---

## Usage

1. Launch PRISM — it will automatically detect any local Ollama models.
2. Select one or more models to benchmark, or explore the **bundled baseline results** immediately.
3. Run a benchmark against **ARC‑Challenge** or **SciQ** — PRISM generates five semantically equivalent prompt variants (P₀–P₄) per question.
4. View results in the analytics dashboard: KPI cards, per-question drill-down, and side-by-side model comparison.
5. Export a **PDF report** of your results, or opt in to **sync anonymized runs** to the community results hub for maintainer review.

---

## Benchmark Methodology

| | |
|---|---|
| **Datasets** | ARC‑Challenge, SciQ |
| **Prompt Variants** | P₀–P₄ (semantically equivalent rephrasings) |
| **Inference** | Local Ollama models only |
| **Scoring** | Accuracy, consistency, prompt sensitivity, deviation, format adherence |
| **Storage** | SQLite locally, with optional Supabase sync |

### Crash-safe execution

PRISM distinguishes between recoverable and fatal inference failures:

- **Recoverable** — invalid output, refusals, malformed responses, single HTTP failures → recorded as `UNKNOWN`, benchmark continues.
- **Fatal** — Ollama server crash, OOM termination, server unreachable, repeated consecutive failures → progress is saved immediately, and you can **continue**, **restart**, or **stop safely**. No completed results are ever lost.

---

## Project Structure

```text
prism/
├── app/                    # Desktop GUI (PySide6)
├── prism_core/             # Benchmark engine — datasets, prompts, inference, scoring, storage
├── datasets/               # ARC-Challenge & SciQ datasets
├── reports/                # PDF report generation
├── packaging/              # Platform installer configs
├── scripts/                # Maintainer utilities
├── supabase/               # Database schema for optional cloud sync
├── resources/              # Bundled assets
├── main.py                 # App entry point
└── prism.spec               # PyInstaller build spec
```

### Module overview

| Module | Purpose |
|---|---|
| **prism_core** | Dataset loading, prompt generation, Ollama inference, response parsing, scoring, reporting, SQLite storage |
| **Desktop GUI** | Dashboard, benchmark workflow, analytics, model comparison, report viewer |
| **Ollama Integration** | Model detection, server health checks, pull progress, inference execution |
| **Supabase Sync** | Optional cloud synchronization of benchmark runs (publishable key, Row Level Security) |
| **Public Results Hub** | Downloads maintainer-approved benchmark snapshots from GitHub |

### How cloud sync & publishing work

If Supabase credentials are configured, completed runs are uploaded as **unapproved** submissions using the publishable key, protected by Row Level Security. Maintainers review submissions and publish approved results to the public dataset repository, so every PRISM installation can discover verified baselines without needing direct database access.

**Public Results Repository:** https://github.com/Nabin-16/Reliability-test-result-model-versions

---

## 🤝 Contributing

Contributions are welcome! Whether it's a bug fix, a new dataset, a new scoring metric, or UI polish:

1. **Fork** the repository and create your branch from `main`.
2. Set up a dev environment following [Option 2 — Run from source](#option-2--run-from-source).
3. Make your changes, and add/update tests where applicable.
4. Make sure the app still launches cleanly and existing benchmarks run without errors.
5. Open a **pull request** describing what changed and why.

Please don't include real Supabase secret/service-role keys, personal Ollama data, or large binary artifacts in your commits. Bug reports and feature requests are welcome via **GitHub Issues**.



## Acknowledgements

- [Ollama](https://ollama.com) for local LLM inference
- ARC‑Challenge and SciQ dataset authors
- The PRISM community for benchmark submissions and verified results
