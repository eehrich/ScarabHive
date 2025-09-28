# Web Research Agent Plugin

The Web Research Agent plugin provides advanced web research capabilities including comprehensive topic research, fact-checking, source comparison, and intelligent query processing. It combines multiple search sources and analytical approaches for thorough information gathering.

## Overview

This plugin acts as an intelligent research assistant that can perform complex web research tasks, verify claims against multiple sources, compare information across different websites, and provide comprehensive analysis of topics with source attribution.

## Features

### Core Operations
- **Topic Research**: Comprehensive multi-source research on any topic
- **Fact Checking**: Verify claims against reliable sources
- **Source Comparison**: Compare information across multiple websites
- **Intelligent Queries**: General research with smart source selection
- **Citation Management**: Proper source attribution and linking

### Advanced Capabilities
- **Multi-Source Analysis**: Combines results from multiple search engines
- **Reliability Assessment**: Evaluates source credibility and authority
- **Bias Detection**: Identifies potential bias in sources
- **Contradiction Analysis**: Highlights conflicting information
- **Summary Generation**: Creates comprehensive research summaries

## Configuration

Configure the Web Research Agent plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - web_research_agent

servers:
  web_research_agent:
    type: web_research_agent
    max_steps: 20
    # Optional configuration
    # default_max_results: 10
    # timeout: 60
```

### Environment Variables
- `RESEARCH_TIMEOUT`: Default research timeout (default: 60 seconds)
- `RESEARCH_MAX_SOURCES`: Maximum sources per research task
- `GOOGLE_API_KEY`: Google Search API key (if using Google)
- `BING_API_KEY`: Bing Search API key (if using Bing)

## Usage Examples

### Topic Research
```python
# Comprehensive topic research
{
  "topic": "renewable energy technologies 2025",
  "max_results": 15
}

# Research with specific sources
{
  "topic": "artificial intelligence ethics",
  "source_urls": [
    "https://ethics.ai.org",
    "https://www.fhi.ox.ac.uk"
  ],
  "max_results": 10
}
```

### Fact Checking
```python
# Verify a specific claim
{
  "claim": "Solar energy is now cheaper than fossil fuels in most countries",
  "max_results": 8
}

# Fact-check with preferred sources
{
  "claim": "Electric vehicles have lower lifetime emissions than gas cars",
  "source_urls": [
    "https://www.iea.org",
    "https://www.epa.gov"
  ]
}
```

### Source Comparison
```python
# Compare sources on a topic
{
  "topic": "climate change causes",
  "source_urls": [
    "https://climate.nasa.gov",
    "https://www.ipcc.ch",
    "https://www.noaa.gov"
  ],
  "max_results": 12
}

# Compare news coverage
{
  "topic": "latest AI breakthrough",
  "max_results": 20
}
```

### General Research Queries
```python
# Intelligent research query
{
  "task": "What are the environmental benefits and drawbacks of nuclear energy?",
  "max_results": 10
}

# Technical research
{
  "task": "Compare different machine learning algorithms for natural language processing",
  "max_results": 15
}
```

## API Reference

### Tools

#### research
Performs comprehensive topic research with multiple sources.

**Parameters:**
- **topic** (required): The topic to research
- **source_urls**: Optional specific URLs to include
- **max_results**: Maximum number of results (default: 5)

#### fact_check  
Verifies claims against multiple reliable sources.

**Parameters:**
- **claim** (required): The claim to verify
- **source_urls**: Optional specific URLs to check against
- **max_results**: Maximum number of sources to check (default: 5)

#### compare_sources
Compares information across multiple sources for a topic.

**Parameters:**
- **topic** (required): The topic to compare sources for
- **source_urls**: Specific URLs to compare
- **max_results**: Maximum number of sources (default: 5)

#### ask
General research query with intelligent source selection.

**Parameters:**
- **task** (required): The research task or query to perform
- **max_results**: Maximum number of results (default: 5)

### Response Format

#### Research Response
```json
{
  "topic": "renewable energy technologies 2025",
  "summary": "Renewable energy technologies continue to advance rapidly in 2025, with significant improvements in solar, wind, and storage technologies...",
  "sources": [
    {
      "title": "Solar Energy Advances in 2025",
      "url": "https://example.com/solar-2025",
      "domain": "example.com",
      "credibility_score": 8.5,
      "relevance_score": 9.2,
      "content_snippet": "Latest developments in solar technology show 23% efficiency improvements...",
      "key_points": [
        "23% efficiency improvement in solar panels",
        "Cost reduction of 15% compared to 2024",
        "New perovskite-silicon tandem cells"
      ],
      "publication_date": "2025-01-10",
      "author": "Dr. Jane Smith"
    }
  ],
  "key_findings": [
    "Solar panel efficiency has improved by 23% in 2025",
    "Wind energy capacity has doubled in developing nations", 
    "Battery storage costs have dropped 30% year-over-year"
  ],
  "consensus_points": [
    "Renewable energy is becoming cost-competitive with fossil fuels",
    "Storage technology is crucial for grid reliability"
  ],
  "conflicting_information": [
    {
      "topic": "Nuclear energy role in clean transition",
      "viewpoint_a": "Nuclear is essential for baseload clean power",
      "viewpoint_b": "Renewables and storage can replace nuclear",
      "sources_a": ["source1.com", "source2.com"],
      "sources_b": ["source3.com", "source4.com"]
    }
  ],
  "research_quality": {
    "total_sources": 12,
    "peer_reviewed_sources": 5,
    "recent_sources": 8,
    "diverse_perspectives": true,
    "bias_assessment": "minimal_bias"
  }
}
```

#### Fact Check Response
```json
{
  "claim": "Solar energy is now cheaper than fossil fuels in most countries",
  "verification_result": "MOSTLY_TRUE",
  "confidence_score": 8.7,
  "supporting_evidence": [
    {
      "source": "International Energy Agency",
      "url": "https://iea.org/solar-costs-2025",
      "evidence": "Solar PV costs have fallen 85% since 2010, making it cheapest electricity source in most markets",
      "credibility": 9.5
    }
  ],
  "contradicting_evidence": [
    {
      "source": "Energy Market Analysis",
      "url": "https://example.com/energy-costs",
      "evidence": "When including storage costs, solar is still more expensive in some regions",
      "credibility": 7.2
    }
  ],
  "context": "The claim is generally accurate for electricity generation costs, but total system costs including storage may vary by region",
  "verification_sources": 8,
  "fact_check_date": "2025-01-15"
}
```

## Research Quality Indicators

### Source Credibility Assessment
- **Domain Authority**: Website reputation and trustworthiness
- **Author Expertise**: Credentials and expertise in the field
- **Publication Quality**: Peer review status and editorial standards
- **Citation Count**: How often the source is cited by others
- **Recency**: How current the information is

### Bias Detection
- **Political Bias**: Left/right political leanings
- **Commercial Bias**: Corporate or financial interests
- **Confirmation Bias**: Selective presentation of evidence
- **Geographic Bias**: Regional or cultural perspectives
- **Temporal Bias**: Historical context and timing

### Information Reliability
- **Primary vs Secondary**: Direct sources vs. reporting on sources  
- **Peer Review Status**: Academic or editorial review process
- **Fact-Checking**: Verified by fact-checking organizations
- **Cross-Verification**: Confirmed by multiple independent sources
- **Expert Consensus**: Agreement among domain experts

## Research Methodologies

### Multi-Source Triangulation
1. **Diverse Source Types**: Academic, news, government, NGO sources
2. **Geographic Diversity**: Sources from different countries/regions
3. **Temporal Spread**: Both recent and historical perspectives
4. **Methodology Variety**: Different research approaches and data

### Claim Verification Process
1. **Claim Decomposition**: Break complex claims into verifiable parts
2. **Source Identification**: Find authoritative sources for each part
3. **Evidence Evaluation**: Assess quality and relevance of evidence
4. **Contradiction Analysis**: Identify and explain conflicting information
5. **Synthesis**: Provide balanced conclusion with confidence level

### Topic Research Framework
1. **Background Research**: Establish basic understanding
2. **Key Question Identification**: Define specific research questions  
3. **Source Discovery**: Find relevant and credible sources
4. **Information Synthesis**: Combine and analyze information
5. **Gap Identification**: Identify areas needing more research

## Best Practices

### Query Formulation
- **Specific Topics**: Use precise terms for better results
- **Multiple Angles**: Research different aspects of complex topics
- **Neutral Language**: Avoid biased or leading questions
- **Scope Definition**: Clearly define the research scope

### Source Selection
- **Diversity Priority**: Include varied perspectives and source types
- **Quality Over Quantity**: Prefer fewer high-quality sources
- **Recent vs Historical**: Balance current and historical sources
- **Expert Sources**: Prioritize domain experts and authorities

### Critical Analysis
- **Verify Claims**: Cross-check important claims across sources
- **Identify Conflicts**: Note and explain contradicting information
- **Assess Bias**: Consider potential biases in sources
- **Context Matters**: Consider broader context and implications

## Limitations and Considerations

### Technical Limitations
- **Language Barriers**: Primarily English-language sources
- **Paywall Content**: Limited access to subscription content  
- **Real-time Data**: Information may not be real-time current
- **Deep Web**: Cannot access private or password-protected content

### Methodological Considerations
- **Source Availability**: Research quality depends on available sources
- **Algorithmic Bias**: Search algorithms may introduce bias
- **Temporal Relevance**: Information currency varies by topic
- **Cultural Context**: May reflect Western/English-speaking perspectives

### Ethical Guidelines
- **Attribution**: Always provide proper source attribution
- **Fair Use**: Respect copyright and fair use principles
- **Privacy**: Avoid researching private individuals without consent
- **Misinformation**: Clearly label unverified or disputed information

## Troubleshooting

### Common Issues

#### Poor Research Quality
- **Broad Topics**: Make research queries more specific
- **Limited Sources**: Expand search terms or time ranges
- **Bias Issues**: Actively seek diverse perspectives
- **Outdated Information**: Focus on recent sources for current topics

#### Fact-Checking Difficulties
- **Subjective Claims**: Focus on factual, verifiable statements
- **Emerging Topics**: Allow for uncertainty in rapidly evolving areas
- **Complex Claims**: Break down into smaller, verifiable parts
- **Conflicting Sources**: Present multiple perspectives fairly

#### Technical Problems
- **Timeout Issues**: Reduce max_results or simplify queries
- **Source Access**: Check for connectivity and access issues  
- **Rate Limiting**: Implement delays between requests
- **API Limits**: Monitor and manage API usage quotas

## Cancellation and graceful shutdown

This plugin supports cooperative cancellation via the AgentSystem cancellation contract. When the agent requests cancellation the plugin will receive a `_cancellation_token` in the call `params`. Long-running operations should check `token.is_cancelled` and abort early, and may register short cleanup callbacks using `token.add_cleanup_callback()`.

Example (check token in a long-running loop):

```python
token = params.get("_cancellation_token")
if token and token.is_cancelled:
  return {"status": "cancelled", "request_id": request_id, "forced": token.is_forced}

# register cleanup
async def _cleanup():
  ...
if token:
  token.add_cleanup_callback(_cleanup)

# inside loop
if token and token.is_cancelled:
  await token.cleanup()
  return {"status": "cancelled", "request_id": request_id, "forced": token.is_forced}
```