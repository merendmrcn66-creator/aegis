# AEGIS Agent

A code-focused, file-controlling, multi-API-key (auto quota-rotating) **Gemini chat agent**. Runs as a single desktop GUI application (built with CustomTkinter).

## Features

- **Code-focused system prompt** — file creation, editing, and deletion; running terminal commands
- **Multi API key support** — automatic quota rotation across an unlimited number of Gemini API keys
- **Auto-recovery** — keys that run out of quota are automatically retested and reactivated in the background every hour
- **Centralized settings window** — manage keys, model, response language, and browser mode from one place
- **File attachments** — attach images, PDFs, code, text, audio, video, etc. via a file picker
- **Built-in browser control** — real-time browser monitoring with a virtual cursor overlay and a persistent browser profile (Playwright-based)
- **Git integration** — the agent can commit/branch/diff and open PRs on its own request (via GitHub CLI)
- **Usage tracking** — per-key token/cost tracking with estimated USD cost
- **Chat history** — save, list, and reload past sessions
- **Automatic test/lint** — optionally run tests/linting automatically after file edits
- **Clipboard image paste** — via Pillow's ImageGrab module (works best on Windows/macOS)
- **Multi-model support** — switch between `lite`, `flash`, and `pro` models
- **Multi-language responses** — Turkish / English / auto-detect

> Note: Some features such as parallel task execution, voice commands (STT/TTS), file watching (watchdog), memory search, and the plugin system are ready on the backend, but triggering them from the GUI is planned for a future update (currently available only in the terminal version).

## Installation

### Core dependencies
```bash
pip install -r requirements.txt
```

### Browser control (optional)
```bash
playwright install
```

## Usage

```bash
python aegis.py
```

## Requirements

- Python 3.9+
- One or more Google Gemini API keys

## License

This is a personal/experimental agent project. Check with the repository owner for usage terms.
