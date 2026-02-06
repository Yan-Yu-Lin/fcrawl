# Serper.dev Documentation Links

> Serper.dev - "The World's Fastest and Cheapest Google Search API"
> - Delivery: 1-2 seconds
> - Free tier: 2,500 queries (no credit card required)
> - Pricing: ~$4/1000 queries

## Official Resources

| Resource | URL |
|----------|-----|
| Homepage | https://serper.dev |
| Playground | https://serper.dev/playground |
| Dashboard | https://serper.dev/dashboard |

**Note:** Serper.dev is heavily JS-rendered. Their "documentation" is primarily the playground and integrations.

## API Endpoints

Base URL: `https://google.serper.dev`

| Endpoint | Path | Description |
|----------|------|-------------|
| Web Search | `/search` | General Google search results |
| Images | `/images` | Google Images search |
| News | `/news` | Google News search |
| Maps | `/maps` | Google Maps search |
| Places | `/places` | Google Places search |
| Videos | `/videos` | Video search results |
| Shopping | `/shopping` | Google Shopping search |
| Scholar | `/scholar` | Google Scholar search |
| Patents | `/patents` | Google Patents search |
| Autocomplete | `/autocomplete` | Search suggestions |
| Scrape | `/scrape` | Webpage content extraction |

## API Parameters

### Required
- `q` - Search query string

### Optional
| Parameter | Description | Example |
|-----------|-------------|---------|
| `gl` | Country code for localized results | `us`, `gb`, `fr`, `jp` |
| `hl` | Language code | `en`, `ja`, `zh-TW` |
| `location` | Geographic location | `Tokyo, Japan` |
| `num` | Number of results (default: 10) | `20` |
| `page` | Pagination | `1`, `2`, `3` |
| `autocorrect` | Auto-correct spelling | `true`/`false` |

### Advanced Search Operators
| Operator | Description | Example |
|----------|-------------|---------|
| `site:` | Limit to specific domain | `site:github.com` |
| `filetype:` | Limit to file type | `filetype:pdf` |
| `inurl:` | Word in URL | `inurl:api` |
| `intitle:` | Word in title | `intitle:documentation` |
| `related:` | Find similar websites | `related:example.com` |
| `before:` | Date before (YYYY-MM-DD) | `before:2024-01-01` |
| `after:` | Date after (YYYY-MM-DD) | `after:2023-06-01` |
| `exact:` | Exact phrase match | `exact:"hello world"` |
| `exclude:` | Exclude terms | `exclude:spam` |
| `or:` | OR operator | `python or javascript` |

## Response Format (JSON)

```json
{
  "searchParameters": { "q": "query", "gl": "us", "hl": "en" },
  "knowledgeGraph": { "title": "...", "description": "..." },
  "organic": [
    {
      "title": "Result Title",
      "link": "https://example.com",
      "snippet": "Description text...",
      "position": 1
    }
  ],
  "peopleAlsoAsk": [
    { "question": "...", "answer": "..." }
  ],
  "relatedSearches": [
    { "query": "related term" }
  ]
}
```

## Authentication

```bash
# Environment variable
export SERPER_API_KEY="your_api_key_here"

# HTTP Header
X-API-KEY: your_api_key_here
```

## Example Usage (cURL)

```bash
curl -X POST 'https://google.serper.dev/search' \
  -H 'X-API-KEY: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"q": "apple inc"}'
```

## Example Usage (Python)

```python
import requests

url = "https://google.serper.dev/search"
headers = {
    "X-API-KEY": "YOUR_API_KEY",
    "Content-Type": "application/json"
}
data = {"q": "python tutorials", "num": 10}

response = requests.post(url, headers=headers, json=data)
results = response.json()

for item in results.get("organic", []):
    print(f"{item['title']}: {item['link']}")
```

---

## Integration Documentation

### LLM Frameworks

| Framework | Documentation URL |
|-----------|-------------------|
| CrewAI | https://docs.crewai.com/en/tools/search-research/serperdevtool |
| LangChain | https://python.langchain.com/docs/integrations/providers/google_serper |
| Haystack | https://docs.haystack.deepset.ai/docs/serperapigooglesearch |

### Workflow Automation

| Platform | Documentation URL |
|----------|-------------------|
| Draft & Goal | https://docs.dng.ai/tools/serper-dev |
| n8n | https://docs.n8n.io/integrations/builtin/credentials/serp/ |

### MCP Servers (Claude Code / Cursor / Cline)

| Server | Repository |
|--------|------------|
| serper-search-scrape | https://github.com/marcopesani/mcp-server-serper |
| Serper-search-mcp | https://github.com/NightTrek/Serper-search-mcp |
| serper-mcp-server | https://github.com/garylab/serper-mcp-server |
| kindly-web-search | https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server |

### MCP Server Installation (Claude Code)

```bash
# Via Smithery
npx -y @smithery/cli install @marcopesani/mcp-server-serper --client claude
```

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "serper-search": {
      "command": "npx",
      "args": ["-y", "serper-search-scrape-mcp-server"],
      "env": {
        "SERPER_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

---

## Comparison with Alternatives

| Service | Speed | Free Tier | Price/1000 | Notes |
|---------|-------|-----------|------------|-------|
| Serper.dev | 1-2s | 2,500 | ~$4 | Real Google SERP |
| Tavily | 1-2s | 1,000/mo | $5 | AI-optimized results |
| SerpAPI | 2-3s | 100/mo | $50 | Multiple engines |
| Google Custom Search | 0.5s | 100/day | $5 | NOT real Google (limited index) |

---

*Last updated: 2026-01-31*
