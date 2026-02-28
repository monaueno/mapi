# mapi

Find the exact API call you need with a simple command.

## Install

```bash
pip install git+https://github.com/monaueno/mapi.git
```

## Setup

Get a free Groq API key at [console.groq.com](https://console.groq.com), then:

```bash
export GROQ_API_KEY='your_key'
```

## Usage

```bash
mapi -<language> <api> <what you need>
```

### Examples

```bash
mapi -python google maps get directions
mapi -javascript gemini generate content
```

**Any language works.** Just change the flag:

```bash
mapi -python google maps get directions
mapi -rust google maps get directions
mapi -go google maps get directions
```

## How It Works

1. You type what you need in plain English
2. mapi checks its cache — if someone's asked before, you get an instant result
3. If not, it reads the pre-scraped API docs and uses AI to generate the exact HTTP request
4. The endpoint is verified against the real API to make sure it exists
5. The result is cached so the next person gets it instantly

## Test With Your Real API Key

```bash
mapi test api_key="YOUR_REAL_KEY"
```

Runs the last generated request with your actual key and shows the response.

## Supported APIs

- **Gemini** — generate content, generate content with image, embed content, list models, count tokens 
- **Google Maps** — geocoding, directions, nearby search, place details, distance matrix


- **[Firecrawl](https://firecrawl.dev)** — scrapes API documentation pages into clean, structured data
- **[Groq](https://groq.com)** — runs Llama 3.3 70B to find the right endpoint and generate code in any language

## Project Structure

```
mapi/
├── pyproject.toml
├── README.md
└── src/
    └── mapi/
        ├── __init__.py
        ├── main.py            ← all logic in one file
        └── data/
            ├── google_docs.json  ← pre-scraped API docs
            └── cache.json        ← fills up as people use it
```

Most recent result is always stored in last_result.json so mapi always knows that mapi test should test the 

`mapi -python gmail send email
  └─► generates code
  └─► writes to last_result.json (overwrites whatever was there)
  └─► displays output to terminal

mapi test api_key="xyz"
  └─► reads last_result.json
  └─► executes with real key
  └─► displays response`

`last_result.json` might look like:

json

`{
  "api": "gmail",
  "query": "send email",
  "language": "python",
  "endpoint": "POST /gmail/v1/users/me/messages/send",
  "code": "import requests\n\ndef send_email(...)",
  "generated_at": "2026-02-28T..."
}`

One file, always the latest, gets overwritten every time. No history, no complexity.