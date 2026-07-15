import requests
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from google import genai

SOURCE = 'union_leader'
BASE_URL = 'https://www.unionleader.com'
SEARCH_URL = 'https://www.unionleader.com/classifieds/legals/'
SEARCH_PARAMS = [
    ('l', '30'),
    ('q', 'foreclosure'),
    ('c[0]', 'legals'),
    ('m', '1ae34c46-8aa0-11e8-87ed-0baf3e00ee10'),
]

DRUPAL_URL = os.environ['DRUPAL_URL'].rstrip('/')
DRUPAL_USER = os.environ['DRUPAL_USER']
DRUPAL_PASS = os.environ['DRUPAL_PASS']

gemini_client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def fetch_listing_urls():
    urls = []
    for offset in [0, 30]:
        params = SEARCH_PARAMS + [('o', str(offset))]
        response = requests.get(SEARCH_URL, headers=HEADERS, params=params)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.find_all('a', class_='tnt-asset-link'):
            href = link.get('href', '')
            if href and '/classifieds/legals/' in href:
                full_url = BASE_URL + href if href.startswith('/') else href
                if full_url not in urls:
                    urls.append(full_url)
    return urls


def fetch_listing(url):
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    html = response.text
    soup = BeautifulSoup(html, 'html.parser')

    # published_time is embedded in the page's JS data layer
    match = re.search(r'"published_time"\s*:\s*"([^"]+)"', html)
    published_time = match.group(1) if match else None

    # Extract body text using BLOX CMS selectors
    body = None
    for selector in ['div.asset-body', 'div.asset-description', 'div.classified-body']:
        el = soup.select_one(selector)
        if el:
            body = el.get_text(separator='\n', strip=True)
            break

    return published_time, body


def is_within_24_hours(published_time_str):
    if not published_time_str:
        return False
    try:
        published = datetime.fromisoformat(published_time_str.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - published) <= timedelta(hours=24)
    except ValueError:
        return False


def parse_with_gemini(body_text):
    prompt = f"""Extract the following fields from this NH foreclosure notice. Return ONLY a JSON object with exactly these keys:
- mortgagor: the borrower/property owner name(s) being foreclosed on
- mortgagee: the current lender/bank/holder name
- property_address: the full property address (street, city, state)
- sale_date: the auction/sale date in YYYY-MM-DD format, or null if not found
- sale_location: where the sale physically takes place, or null if not stated separately from the property address

Notice text:
{body_text}

Return only the JSON object, nothing else."""

    response = gemini_client.models.generate_content(model='gemini-3.5-flash', contents=prompt)
    text = response.text.strip()
    # Strip markdown code fences if Gemini wraps the JSON
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```\s*$', '', text, flags=re.MULTILINE)
    return json.loads(text.strip())


def notice_exists(source_url):
    response = requests.get(
        f'{DRUPAL_URL}/jsonapi/node/foreclosure',
        headers={'Accept': 'application/vnd.api+json'},
        auth=(DRUPAL_USER, DRUPAL_PASS),
        params={'filter[field_source_url.uri]': source_url},
    )
    if response.status_code == 200:
        return len(response.json().get('data', [])) > 0
    return False


def create_notice(url, published_time, body, fields):
    mortgagor = fields.get('mortgagor') or 'Unknown'
    title = f"Foreclosure Notice - {mortgagor}"
    pub_date = published_time[:10] if published_time else None

    attributes = {
        'title': title,
        'field_mortgagor': fields.get('mortgagor') or '',
        'field_mortgagee': fields.get('mortgagee') or '',
        'field_address': fields.get('property_address') or '',
        'field_sale_location': fields.get('sale_location') or '',
        'field_body': body or '',
        'field_source_url': {'uri': url, 'title': ''},
    }

    if pub_date:
        attributes['field_date_published'] = pub_date

    if fields.get('sale_date'):
        attributes['field_sale_date'] = fields['sale_date']

    payload = {
        'data': {
            'type': 'node--foreclosure',
            'attributes': attributes,
        }
    }

    return requests.post(
        f'{DRUPAL_URL}/jsonapi/node/foreclosure',
        headers={
            'Content-Type': 'application/vnd.api+json',
            'Accept': 'application/vnd.api+json',
        },
        auth=(DRUPAL_USER, DRUPAL_PASS),
        json=payload,
    )


def main():
    print('Fetching Union Leader foreclosure listings...')
    urls = fetch_listing_urls()
    print(f'Found {len(urls)} listing URLs across 2 pages')

    created = skipped_exists = skipped_old = errors = 0

    for url in urls:
        if notice_exists(url):
            print(f'Skip (exists): {url}')
            skipped_exists += 1
            continue

        try:
            published_time, body = fetch_listing(url)
        except Exception as e:
            print(f'Error fetching {url}: {e}')
            errors += 1
            continue

        if not is_within_24_hours(published_time):
            print(f'Skip (older than 24h, published {published_time}): {url}')
            skipped_old += 1
            continue

        if not body:
            print(f'Skip (no body text found): {url}')
            errors += 1
            continue

        try:
            fields = parse_with_gemini(body)
        except Exception as e:
            print(f'Error parsing with Gemini for {url}: {e}')
            errors += 1
            continue

        response = create_notice(url, published_time, body, fields)
        if response.status_code in (200, 201):
            print(f'Created: {fields.get("mortgagor", "Unknown")} - {fields.get("property_address", "")}')
            created += 1
        else:
            print(f'Error creating {url}: {response.status_code} - {response.text[:300]}')
            errors += 1

    print(f'\nDone: {created} created, {skipped_exists} skipped (duplicate), {skipped_old} skipped (too old), {errors} errors')
    if errors > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
