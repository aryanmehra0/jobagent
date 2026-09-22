"""Conservative public team-page leads. No social scraping or email guessing."""
from __future__ import annotations
import json
import re
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

from job_agent.contacts.finder import CompanySiteCrawler, employer_website, PLATFORM_DOMAINS, USER_AGENT
from job_agent.contacts.extract import registrable_domain
from job_agent.config.settings import settings
from job_agent.generation import write_json
from job_agent.runtime import exclusive_run, check_cancelled
from job_agent.tailoring.pipeline import ResumeTailoringPipeline


class TeamPageCrawler(CompanySiteCrawler):
    def _get(self, url):
        """Only public employer URLs; no redirects into social or private sites."""
        import ipaddress
        import socket
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        if registrable_domain(parsed.hostname) in PLATFORM_DOMAINS:
            return None
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                return None
            with self.session.get(url, timeout=self.timeout, allow_redirects=False, stream=True) as response:
                if response.status_code != 200 or not any(t in response.headers.get('Content-Type', '') for t in ('html', 'text')):
                    return None
                return response.raw.read(1_000_000, decode_content=True).decode(response.encoding or 'utf-8', errors='replace')
        except (OSError, ValueError):
            return None


def extract_people(page, source_url, role, company=''):
    parsed = urlparse(source_url)
    if registrable_domain(parsed.netloc) in PLATFORM_DOMAINS or not re.search(r'team|about|leadership|people', parsed.path, re.I):
        return []
    soup = BeautifulSoup(page, 'html.parser')
    visible = ' '.join(soup.stripped_strings)
    candidates = []
    def visit(value, employee=False):
        if isinstance(value, list):
            for item in value:
                visit(item, employee)
        elif isinstance(value, dict):
            if value.get('@type') == 'Person':
                employer = value.get('worksFor', {})
                employer = employer.get('name', '') if isinstance(employer, dict) else str(employer)
                if (employer and employer.casefold() == company.casefold()) or (employee and not employer):
                    candidates.append((value.get('name', ''), value.get('jobTitle', '')))
            for key in ('@graph', 'employee', 'member'):
                if key in value:
                    visit(value[key], employee=key in ('employee', 'member'))
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or ''))
        except ValueError:
            pass
    for card in soup.select('[itemtype$="/Person"], .team-member, .team_member'):
        name = card.select_one('[itemprop="name"], .name, .member-name')
        title = card.select_one('[itemprop="jobTitle"], .job-title, .role, .position')
        if name and title:
            candidates.append((name.get_text(' ', strip=True), title.get_text(' ', strip=True)))
    team = ('product',) if 'product' in role.lower() else ('design',) if 'design' in role.lower() else ('engineer', 'technology', 'technical', 'data', 'machine learning')
    by_name = {}
    for name, title in candidates:
        if not isinstance(name, str) or not isinstance(title, str):
            continue
        if not (2 <= len(name.split()) <= 4 and all(re.fullmatch(r"[^\W\d_][^\W\d_'’-]*['’-]?[^\W\d_]*", word, re.UNICODE) for word in name.split())):
            continue
        if set(name.lower().split()) & {'team', 'customer', 'support', 'engineer', 'engineering', 'firstname', 'lastname', 'contact', 'unknown'}:
            continue
        if name not in visible or title not in visible or not any(word in title.lower() for word in team):
            continue
        by_name.setdefault(name, set()).add(title)
    return [{'name': name, 'title': next(iter(titles)), 'source_url': source_url,
             'confidence': 'Unverified public team lead; no relationship or referral is established'}
            for name, titles in by_name.items() if len(titles) == 1]


@exclusive_run
def discover(*, job_id=None, limit=5, crawler=None):
    from job_agent.tracking.export import _read_json
    jobs = ResumeTailoringPipeline._load_qualified(settings.outputs_dir/'qualified_jobs.json')
    crawler = crawler or TeamPageCrawler(max_pages=4)
    path = settings.outputs_dir/'warm_contacts.json'
    found = _read_json(path, {})
    checked = 0
    for item in [j for j in jobs if job_id is None or j.job.id == job_id][:limit]:
        check_cancelled()
        job = item.job
        root = employer_website(job)
        found[job.id] = []
        if not root:
            continue
        robots = crawler._robots(root)
        home = crawler._get(root) if robots.can_fetch(USER_AGENT, root) else ''
        urls = [urljoin(root, '/team'), urljoin(root, '/about')]
        urls += [u for u in crawler._candidate_urls(root, home) if re.search(r'team|about|leadership|people', urlparse(u).path, re.I)]
        leads = {}
        for url in list(dict.fromkeys(urls))[:4]:
            check_cancelled()
            if robots.can_fetch(USER_AGENT, url):
                for person in extract_people(crawler._get(url) or '', url, job.title, job.company):
                    leads.setdefault((person['name'], person['title']), person)
        titles = {}
        for person in leads.values():
            titles.setdefault(person['name'].casefold(), set()).add(person['title'])
        found[job.id] = [p for p in leads.values() if len(titles[p['name'].casefold()]) == 1][:10]
        checked += 1
        write_json(path, found)
    write_json(path, found)
    return {'companies_checked': checked, 'leads': sum(len(v) for v in found.values()), 'note': 'No emails guessed or sent.'}
