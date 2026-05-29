# Voice to Text

A dead-simple, **100% free** way to talk-to-type anywhere on Windows — Gmail, your browser, Word, chat, any text box — using the voice typing that's **already built into Windows** (`Win` + `H`).

Think of it as a lightweight, no-cost alternative to paid dictation apps like Wispr Flow. There's nothing to sign up for, no API keys, no cloud service, and no subscription. It simply makes Windows' own Voice Typing one keypress (or one click) away.

## Why this exists

Paid dictation tools are great until the trial ends. Meanwhile, Windows 10 and 11 ship with a genuinely good voice typing engine that works system-wide — most people just don't know it's there. This repo is a friendly README plus a tiny one-click launcher so you never go back to a paid tool for basic dictation.

## How to use it

**The easiest way (recommended):**

1. Click into any text box — an email, a search bar, anywhere.
2. Press `Windows key` + `H` together.
3. Talk. Your words are typed in. Press `Win` + `H` again (or close the little bar) to stop.

That's it. It works in every app, offline, for free.

**One-click shortcut (optional):**

Double-click `Open Voice Typing.bat` to pop the voice bar open without touching the keyboard. Click into your text box *first*, then run it, so your words land in the right place. (Honestly, pressing `Win` + `H` where you're already typing is simpler.)

## Tip: auto punctuation

Open Voice Typing (`Win` + `H`), click the gear icon on the little bar, and turn on **auto-punctuation** so it adds commas and periods for you.

## What's in here

| File | What it does |
| --- | --- |
| `Open Voice Typing.bat` | Double-click launcher that opens Windows Voice Typing. |
| `open-voice-typing.ps1` | The tiny PowerShell script it runs (simulates `Win` + `H`). |
| `README.md` | This file. |

## Requirements

- Windows 10 or Windows 11 (Voice Typing is built in)
- A microphone

## How it works

`open-voice-typing.ps1` calls the Windows `keybd_event` API to press `Win` + `H`, which is the system shortcut for Voice Typing. No third-party software, no network calls, nothing runs in the background.

## License

MIT — see [LICENSE](LICENSE).
