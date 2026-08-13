"""Auto-registered tool: web_fetch (scope=shared)."""

from typing import Any, Dict


def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    import requests
    from bs4 import BeautifulSoup
    import json

    url = args.get("url", "")
    max_length = args.get("max_length", 5000)

    if not url:
        return json.dumps({"error": "url is required"}, ensure_ascii=False)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)

        # Auto detect encoding
        if resp.encoding == 'ISO-8859-1':
            resp.encoding = resp.apparent_encoding

        soup = BeautifulSoup(resp.text, 'lxml')

        # Remove unwanted elements
        for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
            tag.decompose()

        # Try to find main content
        title = soup.find('title')
        title_text = title.get_text(strip=True) if title else ""

        # Extract paragraphs
        paragraphs = soup.find_all('p')
        content_parts = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if len(text) > 10:  # Skip short fragments
                content_parts.append(text)

        content = '\n\n'.join(content_parts)

        # If too little content from <p>, try <div> with substantial text
        if len(content) < 200:
            for div in soup.find_all('div'):
                text = div.get_text(strip=True)
                if len(text) > 100:
                    content_parts.append(text)
                    if sum(len(t) for t in content_parts) > max_length:
                        break
            content = '\n\n'.join(content_parts)

        # Truncate to max_length
        if len(content) > max_length:
            content = content[:max_length] + "...[截断]"

        return json.dumps({
            "url": url,
            "title": title_text,
            "status": resp.status_code,
            "content_length": len(content),
            "content": content
        }, ensure_ascii=False, indent=2)

    except requests.exceptions.Timeout:
        return json.dumps({"error": "抓取超时"}, ensure_ascii=False)
    except requests.exceptions.ConnectionError as e:
        return json.dumps({"error": f"连接失败: {str(e)[:100]}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"抓取失败: {str(e)}"}, ensure_ascii=False)
