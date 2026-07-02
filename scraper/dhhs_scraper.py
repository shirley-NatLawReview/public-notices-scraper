import requests
import json
import os
import sys

DHHS_API_URL = 'https://www.dhhs.nh.gov/content/api/news'
DHHS_BASE_URL = 'https://www.dhhs.nh.gov'
DHHS_CATEGORY_ID = '11426'

DRUPAL_URL = os.environ['DRUPAL_URL'].rstrip('/')
DRUPAL_USER = os.environ['DRUPAL_USER']
DRUPAL_PASS = os.environ['DRUPAL_PASS']


def fetch_dhhs_notices():
    notices = []
    page = 1
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Referer': 'https://www.dhhs.nh.gov/news-events/public-notices',
    }

    while True:
        response = requests.get(DHHS_API_URL, headers=headers, params={
            'q': f'@field_press_release_category|=|{DHHS_CATEGORY_ID}',
            'sort': 'field_date|desc|ALLOW_NULLS',
            'view': 'list',
            'page': page,
            'size': 50,
        })
        response.raise_for_status()
        data = response.json()
        last_page = data.get('last_page', 1)

        for item in data.get('data', []):
            fields = item['fields']
            title = fields['title'][0] if fields.get('title') else ''
            date = fields['field_date'][0] if fields.get('field_date') else None

            path_raw = fields.get('path', ['{}'])[0]
            path_data = json.loads(path_raw) if path_raw else {}
            alias = path_data.get('alias', '')
            source_url = DHHS_BASE_URL + alias if alias else ''

            end_date_raw = fields.get('field_closed_date', [None])[0]
            end_date = end_date_raw[:10] if end_date_raw else None

            if title and source_url:
                notices.append({
                    'title': title,
                    'state': 'NH',
                    'county': '',
                    'published_date': date,
                    'end_date': end_date,
                    'source_url': source_url,
                    'source_name': 'NH Dept. of Health and Human Services',
                    'category': 'Public Meetings & Elections',
                })

        if page >= last_page:
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
    payload = {
        'data': {
            'type': 'node--public_notice_scraping',
            'attributes': {
                'title': notice['title'],
                'field_title': notice['title'],
                'field_state': notice['state'],
                'field_county': notice['county'] or '',
                'field_published_date': notice['published_date'],
                'field_category': notice['category'],
                'field_source_name': notice['source_name'],
                'field_source_url': {'uri': notice['source_url'], 'title': ''},
                'field_is_active_notice': True,
                'status': True,
            }
        }
    }

    if notice.get('end_date'):
        payload['data']['attributes']['field_end_date'] = notice['end_date']

    return requests.post(
        f'{DRUPAL_URL}/jsonapi/node/public_notice_scraping',
        headers={
            'Content-Type': 'application/vnd.api+json',
            'Accept': 'application/vnd.api+json',
        },
        auth=(DRUPAL_USER, DRUPAL_PASS),
        json=payload,
    )


def main():
    print('Fetching DHHS notices...')
    notices = fetch_dhhs_notices()
    print(f'Found {len(notices)} notices')

    created = skipped = errors = 0

    for notice in notices:
        if notice_exists(notice['source_url']):
            print(f'Skip (exists): {notice["title"]}')
            skipped += 1
            continue

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
