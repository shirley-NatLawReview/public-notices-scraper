import requests
from curl_cffi import requests as browser_requests
import json
import os
import re
import sys
from datetime import datetime
from bs4 import BeautifulSoup
from openai import OpenAI

DES_API_URL = 'https://www.des.nh.gov/content/api/news'
DES_BASE_URL = 'https://www.des.nh.gov'
DES_NEWS_TYPE_ID = '5896'  # "Public Comment Notices" taxonomy term
CURRENT_YEAR = datetime.now().year

DRUPAL_URL = os.environ['DRUPAL_URL'].rstrip('/')
DRUPAL_USER = os.environ['DRUPAL_USER']
DRUPAL_PASS = os.environ['DRUPAL_PASS']

ai_client = OpenAI(
    base_url='https://models.inference.ai.azure.com',
    api_key=os.environ['GH_MODELS_TOKEN'],
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Referer': 'https://www.des.nh.gov/public-comment-opportunities',
}

# Set to False after the first "field_city not recognized" response so we stop
# retrying every subsequent notice and just log once.
_field_city_supported = True


# Every notice under the DES "Public Comment Notices" taxonomy term is a
# government agency notice (permits, rulemaking, hearings) — the keyword-based
# categorizer used for DHHS's mixed content (estates/tax/school notices) produces
# false positives here (e.g. "Fidelity Real Estate Company" -> foreclosure), so
# every DES notice is filed under the same category.
DES_CATEGORY = 'government_programs'


def parse_locality_with_ai(title, plain_text_body):
    prompt = f"""Extract locality information from this New Hampshire environmental public notice.

Some notices are statewide, regional (e.g. apply to multiple New England states), or otherwise not tied
to one specific place. Others concern a specific facility, project, or site at a specific address, city/town.

Return ONLY a JSON object with exactly these keys:
- county: the New Hampshire county name only, no "County" suffix (e.g. "Hillsborough"), for the county
  where the specific facility/project/site discussed in the notice is located. Return null if the notice
  is statewide, regional, or not tied to one specific location.
- city: the New Hampshire city or town name where the specific facility/project/site is located, or null
  if not applicable / statewide / regional.

Title: {title}

Notice text:
{plain_text_body}"""

    response = ai_client.chat.completions.create(
        model='gpt-4o-mini',
        messages=[
            {'role': 'system', 'content': 'You extract structured locality data from NH environmental public notices. Return only valid JSON, nothing else.'},
            {'role': 'user', 'content': prompt},
        ],
        response_format={'type': 'json_object'},
        max_tokens=200,
    )
    return json.loads(response.choices[0].message.content)


def fetch_des_notices():
    notices = []
    page = 1

    while True:
        response = browser_requests.get(DES_API_URL, impersonate='chrome131', headers=HEADERS, params={
            'q': f'@field_news_type|=|{DES_NEWS_TYPE_ID}',
            'sort': 'field_date|desc|ALLOW_NULLS',
            'view': 'list',
            'page': page,
            'size': 50,
        })
        response.raise_for_status()
        data = response.json()
        last_page = data.get('last_page', 1)

        stop = False
        for item in data.get('data', []):
            fields = item['fields']
            title = fields['title'][0] if fields.get('title') else ''
            date = fields['field_date'][0] if fields.get('field_date') else None

            # Results are sorted by field_date desc (nulls last), so once we hit
            # a notice older than this year everything after it is too — stop.
            if not date or int(date[:4]) < CURRENT_YEAR:
                stop = True
                break

            path_raw = fields.get('path', ['{}'])[0]
            path_data = json.loads(path_raw) if path_raw else {}
            alias = path_data.get('alias', '')
            source_url = DES_BASE_URL + alias if alias else ''

            body_raw = (fields.get('body') or [{}])[0].get('#text') or ''
            # Fix relative URLs so links point back to DES, not our site
            body = re.sub(r'href="(/[^"]+)"', lambda m: f'href="{DES_BASE_URL}{m.group(1)}"', body_raw)
            body = re.sub(r'src="(/[^"]+)"', lambda m: f'src="{DES_BASE_URL}{m.group(1)}"', body)

            if title and source_url:
                notices.append({
                    'title': title,
                    'state': 'nh',
                    'published_date': date,
                    'source_url': source_url,
                    'source_name': 'NH Dept. of Environmental Services',
                    'category': DES_CATEGORY,
                    'body': body,
                })

        if stop or page >= last_page:
            break
        page += 1

    return notices


def notice_exists(source_url):
    response = requests.get(
        f'{DRUPAL_URL}/jsonapi/node/public_notice_scraping',
        headers={'Accept': 'application/vnd.api+json'},
        auth=(DRUPAL_USER, DRUPAL_PASS),
        params={'filter[field_source_url.uri]': source_url},
    )
    if response.status_code == 200:
        return len(response.json().get('data', [])) > 0
    return False


def create_notice(notice):
    global _field_city_supported

    attributes = {
        'title': notice['title'],
        'field_title': notice['title'],
        'field_state': notice['state'],
        'field_county': notice['county'] or '',
        'field_published_date': notice['published_date'],
        'field_category': notice['category'],
        'field_source_name': notice['source_name'],
        'field_source_url': {'uri': notice['source_url'], 'title': ''},
    }

    if notice.get('body'):
        attributes['field_body'] = {
            'value': notice['body'],
            'format': 'basic_html',
        }

    if _field_city_supported and notice.get('city'):
        attributes['field_city'] = notice['city']

    payload = {
        'data': {
            'type': 'node--public_notice_scraping',
            'attributes': attributes,
        }
    }

    response = requests.post(
        f'{DRUPAL_URL}/jsonapi/node/public_notice_scraping',
        headers={
            'Content-Type': 'application/vnd.api+json',
            'Accept': 'application/vnd.api+json',
        },
        auth=(DRUPAL_USER, DRUPAL_PASS),
        json=payload,
    )

    # If the content type has no field_city yet, drop it and retry once so a
    # missing field doesn't block every notice in the run.
    if response.status_code == 422 and 'field_city' in attributes and 'field_city' in response.text:
        print('Warning: field_city not recognized by Drupal — add it to the public_notice_scraping content type. Retrying without it.')
        _field_city_supported = False
        del attributes['field_city']
        response = requests.post(
            f'{DRUPAL_URL}/jsonapi/node/public_notice_scraping',
            headers={
                'Content-Type': 'application/vnd.api+json',
                'Accept': 'application/vnd.api+json',
            },
            auth=(DRUPAL_USER, DRUPAL_PASS),
            json=payload,
        )

    return response


def main():
    print('Fetching DES public comment opportunity listings...')
    notices = fetch_des_notices()
    print(f'Found {len(notices)} notices published in {CURRENT_YEAR}')

    created = skipped = errors = 0

    for notice in notices:
        if notice_exists(notice['source_url']):
            print(f'Skip (exists): {notice["title"]}')
            skipped += 1
            continue

        try:
            plain_text = BeautifulSoup(notice['body'], 'html.parser').get_text(separator='\n', strip=True)
            locality = parse_locality_with_ai(notice['title'], plain_text)
            notice['county'] = locality.get('county')
            notice['city'] = locality.get('city')
            print(f'Parsed locality: county={notice["county"]}, city={notice["city"]}')
        except Exception as e:
            print(f'Error parsing locality with AI for {notice["title"]}: {e}')
            notice['county'] = None
            notice['city'] = None

        response = create_notice(notice)
        if response.status_code in (200, 201):
            print(f'Created: {notice["title"]}')
            created += 1
        else:
            print(f'Error "{notice["title"]}": {response.status_code} - {response.text[:300]}')
            errors += 1

    print(f'\nDone: {created} created, {skipped} skipped, {errors} errors')
    if errors > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
