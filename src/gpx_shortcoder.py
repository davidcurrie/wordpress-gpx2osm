#!/usr/bin/env python3
"""gpx_shortcoder.py

Fetch WordPress posts, find links to .gpx files and insert an OSM shortcode
before paragraphs containing the GPX link. Supports dry-run and updating posts
via the WP REST API using basic auth (username + application password).

Usage examples are in the README.
"""

import argparse
import getpass
import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup


SHORTCODE_TPL = ('[osm_map_v3 map_center="autolat,autolon" zoom="autozoom" '
                 'width="100%" height="450" file_list="{relpath}" '
                 'file_color_list="red" file_title="{title}"]')


def find_gpx_links(html, site_base_url):
    """Return list of tuples (a_tag, gpx_url, title) for each .gpx link found.

    a_tag is the BeautifulSoup tag object for the <a> element.
    gpx_url is the absolute URL to the GPX file.
    title is a short derived title.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        # Normalize to absolute URL and check the path portion for .gpx
        abs_url = urljoin(site_base_url, href)
        path = urlparse(abs_url).path or ''
        if not path.lower().rstrip('/').endswith('.gpx'):
            continue

        # Use the anchor text as the title (prefer user-visible text)
        anchor_text = a.get_text(strip=True)

        # Build a robust regex to find an <a ... href=...>anchor_text</a> in the
        # original HTML source. This handles attribute order/spacing.
        href_pat = re.escape(href)
        inner = re.escape(anchor_text)
        anchor_regex = re.compile(r'<a\b[^>]*\bhref\s*=\s*(?:"|\')?' + href_pat + r'(?:"|\')?[^>]*>\s*' + inner + r'\s*</a>', flags=re.IGNORECASE)
        m = anchor_regex.search(html)
        if not m:
            continue

        # Ensure the matched anchor is the only content on its line (allow optional <p> wrapper)
        idx = m.start()
        matched_str = m.group(0)
        line_start = html.rfind('\n', 0, idx) + 1
        line_end = html.find('\n', idx)
        if line_end == -1:
            line_end = len(html)
        line = html[line_start:line_end]

        # Pattern allows optional surrounding <p> tags and whitespace only
        pattern = r'^\s*(?:<p[^>]*>\s*)?' + re.escape(matched_str) + r'(?:\s*</p>)?\s*$'
        if not re.match(pattern, line, flags=re.IGNORECASE):
            continue

        title = anchor_text
        results.append((a, abs_url, title))
    return results


def compute_relative_path(file_url, post_url):
    """Compute a relative path from the post URL to the file URL.

    For WordPress typical structure, it's safe to compute path relative to the
    post's URL directory. Example: post at /yyyy/mm/dd/post-name/ and file at
    /wp-content/uploads/2013/02/file.gpx -> relative path is ../../../../wp-content/...'
    This function returns a path using '..' segments to reach the root, then the
    absolute path to the file.
    """
    file_parsed = urlparse(file_url)
    post_parsed = urlparse(post_url)

    # Only handle same-netloc; if different, still return absolute path
    if file_parsed.netloc != post_parsed.netloc:
        return file_url

    file_path = file_parsed.path
    post_path = post_parsed.path
    # Count directory depth of post path
    # If post_path ends with '/', treat as directory; otherwise remove last segment
    if post_path.endswith('/'):
        post_dir = post_path
    else:
        post_dir = post_path.rsplit('/', 1)[0] + '/'

    # compute number of segments in post_dir after leading '/'
    post_segments = [s for s in post_dir.split('/') if s]
    # For each segment, we need a '..' to go up
    ups = ['..'] * len(post_segments)
    rel = '/'.join(ups + [file_path.lstrip('/')])
    if not rel:
        rel = file_path
    return rel


def insert_shortcode_into_html(html, link_tag, shortcode):
    """Insert shortcode (plain text) in the HTML before the paragraph that
    contains link_tag. Returns modified HTML string. If paragraph not found,
    insert before the link_tag itself.
    """
    soup = BeautifulSoup(html, "html.parser")
    # find the same tag in the new soup by matching href and text
    target = None
    for a in soup.find_all('a', href=True):
        if a.get('href') == link_tag.get('href') and a.get_text(strip=True) == link_tag.get_text(strip=True):
            target = a
            break

    if target is None:
        return html

    # find enclosing paragraph
    parent_p = target.find_parent('p')
    sc_node = BeautifulSoup(shortcode, 'html.parser')
    if parent_p is not None:
        parent_p.insert_before(sc_node)
    else:
        target.insert_before(sc_node)

    return str(soup)


def get_posts(site_api_url, per_page=100, auth=None):
    """Generator yielding posts from WP REST API /wp/v2/posts?page=N
    Fetches all pages until none left.
    """
    page = 1
    session = requests.Session()
    if auth:
        session.auth = auth
    while True:
        params = {'per_page': per_page, 'page': page}
        # request edit context when authenticated to get raw source (shortcodes unexpanded)
        if auth:
            params['context'] = 'edit'
        resp = session.get(f"{site_api_url.rstrip('/')}/wp/v2/posts", params=params)
        if resp.status_code == 404:
            print("API endpoint not found (404). Check the site URL and API base path.")
            break
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        for p in items:
            yield p
        if 'X-WP-TotalPages' in resp.headers:
            total = int(resp.headers['X-WP-TotalPages'])
            if page >= total:
                break
        else:
            # Heuristic: stop if fewer than per_page items
            if len(items) < per_page:
                break
        page += 1


import json
def update_post(site_api_url, post_id, new_content, auth):
    url = f"{site_api_url.rstrip('/')}/wp/v2/posts/{post_id}"
    resp = requests.post(url, json={'content': new_content}, auth=auth)
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description='Find GPX links in WP posts and add OSM shortcode')
    parser.add_argument('site', help='Site base URL, e.g. http://david.currie.name or https://example.com/wp-json')
    parser.add_argument('--api-base', help='WP REST API base path (default: <site>/wp-json/wp/v2)', default=None)
    parser.add_argument('--user', help='Username for authentication (required unless --dry-run)')
    parser.add_argument('--dry-run', help='Show changes but do not update posts', action='store_true')
    parser.add_argument('--limit', type=int, help='Limit number of posts to process', default=None)
    parser.add_argument('--preview', help='Write updated HTML to local preview/ directory instead of updating site', action='store_true')
    args = parser.parse_args()

    site = args.site.rstrip('/')
    # determine API base
    if args.api_base:
        api_base = args.api_base.rstrip('/')
    else:
        # Derive wp-json base
        if site.endswith('/wp-json'):
            api_base = site
            site = site[: -len('/wp-json')]
        else:
            api_base = site + '/wp-json'

    posts_processed = 0
    updates = []

    auth = None
    # If the user provided credentials, use them (even in preview mode) so
    # preview can fetch raw/source content. Only require credentials when
    # performing live updates and the user did not supply --user.
    if args.user:
        pwd = getpass.getpass(prompt='Application password: ')
        auth = (args.user, pwd)
    else:
        if not args.dry_run and not args.preview:
            parser.error('Authentication required for updates. Provide --user (or use --dry-run / --preview)')

    # If preview mode is enabled, create preview directory
    preview_dir = None
    if args.preview:
        import os
        import shutil
        preview_dir = os.path.join(os.getcwd(), 'preview')
        # Remove any existing content in preview_dir to give a clean preview
        if os.path.exists(preview_dir):
            for name in os.listdir(preview_dir):
                path = os.path.join(preview_dir, name)
                try:
                    if os.path.isfile(path) or os.path.islink(path):
                        os.unlink(path)
                    else:
                        shutil.rmtree(path)
                except Exception:
                    # ignore errors removing individual files
                    pass
        else:
            os.makedirs(preview_dir, exist_ok=True)

    # When auth is provided, pass it to get_posts so we can request context=edit
    for post in get_posts(api_base, per_page=50, auth=auth):
        if args.limit and posts_processed >= args.limit:
            break
        post_id = post.get('id')
        title = post.get('title', {}).get('rendered', '')
        content_obj = post.get('content', {})
        # Use source/raw content if available (requires authenticated edit context)
        if auth and 'raw' in content_obj:
            content = content_obj.get('raw') or content_obj.get('rendered', '')
        else:
            # Fallback to rendered content (shortcodes will be expanded)
            content = content_obj.get('rendered', '')
            # print a visible warning so the user knows source content wasn't available
            print("Warning: using rendered post content (shortcodes may already be expanded).")
        link_matches = find_gpx_links(content, site)
        if not link_matches:
            continue
        print(f"Found {len(link_matches)} gpx link(s) in post {post_id}: {title}")
        new_content = content
        for a_tag, file_url, file_title in link_matches:
            relpath = compute_relative_path(file_url, post.get('link') or post.get('guid', {}).get('rendered', site))
            sc = SHORTCODE_TPL.format(relpath=relpath, title=file_title)
            new_content = insert_shortcode_into_html(new_content, a_tag, sc)

        if new_content != content:
            updates.append({'post_id': post_id, 'title': title, 'old': content, 'new': new_content})
            if args.dry_run:
                print(f"DRY RUN - would update post {post_id} ({title})")
            elif args.preview:
                # Write before/after preview files: preview/<post_id>-<slug>-before.html and -after.html
                import os
                slug = post.get('slug') or str(post_id)
                before_filename = f"{post_id}-{slug}-before.html"
                after_filename = f"{post_id}-{slug}-after.html"
                before_path = os.path.join(preview_dir, before_filename)
                after_path = os.path.join(preview_dir, after_filename)
                try:
                    with open(before_path, 'w', encoding='utf-8') as fh:
                        fh.write(content)
                    with open(after_path, 'w', encoding='utf-8') as fh:
                        fh.write(new_content)
                    print(f"Wrote preview for post {post_id} to {before_path} and {after_path}")
                except Exception as e:
                    print(f"Failed to write preview for post {post_id}: {e}")
            else:
                print(f"Updating post {post_id} ({title})...")
                try:
                    res = update_post(api_base, post_id, new_content, auth)
                    print(f"Updated post {post_id}")
                except Exception as e:
                    print(f"Failed to update post {post_id}: {e}")

        posts_processed += 1

    print('\nSummary:')
    print(f"Posts processed: {posts_processed}")
    print(f"Posts to update: {len(updates)}")


if __name__ == '__main__':
    main()
