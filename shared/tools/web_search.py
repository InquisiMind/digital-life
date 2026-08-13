"""Auto-registered tool: web_search (scope=shared)."""

from typing import Any, Dict


def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    import requests
    import urllib.parse
    from bs4 import BeautifulSoup

    query = args.get("query", "")
    num = args.get("num", 10)

    if not query:
        return "错误：请提供搜索关键词"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
    }

    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count={num}&setlang=zh-CN"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'lxml')
    
        results = []
        items = soup.find_all('li', class_='b_algo')
    
        for item in items[:num]:
            title_tag = item.find('h2')
            link_tag = item.find('a')
            snippet = ""
            snippet_tag = item.find('p')
            if not snippet_tag:
                for div in item.find_all('div'):
                    text = div.get_text(strip=True)
                    if len(text) > 30:
                        snippet = text[:200]
                        break
            else:
                snippet = snippet_tag.get_text(strip=True)[:200]
        
            title = title_tag.get_text(strip=True) if title_tag else ""
            link = link_tag.get('href', '') if link_tag else ""
        
            if title and link:
                results.append(f"{title}\n{snippet}\n{link}")
    
        if not results:
            return f"未找到与 '{query}' 相关的搜索结果"
    
        return f"🔍 搜索 '{query}' — 找到 {len(results)} 条结果\n\n" + "\n\n---\n\n".join(results)

    except Exception as e:
        return f"搜索失败: {str(e)}"
