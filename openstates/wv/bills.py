
import os
import re
import json
import datetime
import collections
from urlparse import urlunparse, urlparse, parse_qsl
from urllib import urlencode, quote, unquote_plus

import lxml.html

from billy.scrape.utils import convert_pdf
from billy.scrape.bills import BillScraper, Bill
from billy.scrape.votes import Vote
import scrapelib


class _Url(object):
    '''A url object that can be compared with other url orbjects
    without regard to the vagaries of casing, encoding, escaping,
    and ordering of parameters in query strings.'''

    def __init__(self, url):
        parts = urlparse(url.lower())
        _query = frozenset(parse_qsl(parts.query))
        _path = unquote_plus(parts.path)
        parts = parts._replace(query=_query, path=_path)
        self.parts = parts

    def __eq__(self, other):
        return self.parts == other.parts

    def __hash__(self):
        return hash(self.parts)


class WVBillScraper(BillScraper):
    state = 'wv'

    bill_types = {'B': 'bill',
                  'R': 'resolution',
                  'CR': 'concurrent resolution',
                  'JR': 'joint resolution'}

    def scrape(self, chamber, session):
        if chamber == 'lower':
            orig = 'h'
        else:
            orig = 's'

        # Scrape the legislature's FTP server to figure out the filenames
        # of bill version documents.
        self._get_version_filenames(session, chamber)

        # scrape bills
        url = ("http://www.legis.state.wv.us/Bill_Status/"
               "Bills_all_bills.cfm?year=%s&sessiontype=RS"
               "&btype=bill&orig=%s" % (session, orig))
        page = lxml.html.fromstring(self.urlopen(url))
        page.make_links_absolute(url)

        for link in page.xpath("//a[contains(@href, 'Bills_history')]"):
            bill_id = link.xpath("string()").strip()
            title = link.xpath("string(../../td[2])").strip()
            self.scrape_bill(session, chamber, bill_id, title,
                             link.attrib['href'])

        # scrape resolutions
        res_url = ("http://www.legis.state.wv.us/Bill_Status/res_list.cfm?"
                   "year=%s&sessiontype=rs&btype=res") % session
        doc = lxml.html.fromstring(self.urlopen(res_url))
        doc.make_links_absolute(res_url)

        # check for links originating in this house
        for link in doc.xpath('//a[contains(@href, "houseorig=%s")]' % orig):
            bill_id = link.xpath("string()").strip()
            title = link.xpath("string(../../td[2])").strip()
            self.scrape_bill(session, chamber, bill_id, title,
                             link.attrib['href'])

    def scrape_bill(self, session, chamber, bill_id, title, url):
        html = self.urlopen(url)

        page = lxml.html.fromstring(html)
        page.make_links_absolute(url)

        bill_type = self.bill_types[bill_id.split()[0][1:]]

        bill = Bill(session, chamber, bill_id, title, type=bill_type)
        bill.add_source(url)

        for version in self.scrape_versions(session, chamber, page, bill_id):
            bill.add_version(**version)

        subjects = []
        # skip first 'Subject' link
        for link in page.xpath("//a[contains(@href, 'Bills_Subject')]")[1:]:
            subject = link.xpath("string()").strip()
            subjects.append(subject)
        bill['subjects'] = subjects

        sponsor_links = page.xpath("//a[contains(@href, 'Bills_Sponsors')]")
        for link in sponsor_links[1:]:
            sponsor = link.xpath("string()").strip()
            bill.add_sponsor('sponsor', sponsor)

        # resolutions
        if bill_type != 'bill':
            if 'SPONSOR(S)' not in html:
                self.warning('incomplete page %s, trying again' % url)
                html = self.urlopen(url)
                page = lxml.html.fromstring(html)
                page.make_links_absolute(url)
                if 'SPONSOR(S)' not in html:
                    self.warning('incomplete page %s, giving up' % url)
                    return

            # sometimes (resolutions only?) there aren't links so we have to
            # use a regex to get sponsors
            block = page.xpath('//div[@id="bhistleft"]')[0].text_content()

            # just text after sponsors
            lines = block.split('SPONSORS:')[1].strip().splitlines()
            for line in lines:
                line = line.strip()
                # until we get a blank line
                if not line:
                    break
                for sponsor in line.split(', '):
                    bill.add_sponsor('sponsor', sponsor.strip())

        for link in page.xpath("//a[contains(@href, 'votes/house')]"):
            self.scrape_vote(bill, link.attrib['href'])

        actor = chamber
        for tr in reversed(page.xpath("//table[@class='tabborder']/descendant::tr")[1:]):
            tds = tr.xpath('td')
            if len(tds) < 3:
                continue

            # order varies on resolutions
            if bill_type == 'bill':
                date = tds[2].text_content().strip()
            else:
                date = tds[0].text_content().strip()

            date = datetime.datetime.strptime(date, "%m/%d/%y").date()
            action = tds[1].text_content().strip()

            if (action == 'Communicated to Senate' or
                action.startswith('Senate received') or
                action.startswith('Ordered to Senate')):
                actor = 'upper'
            elif (action == 'Communicated to House' or
                  action.startswith('House received') or
                  action.startswith('Ordered to House')):
                actor = 'lower'

            if action == 'Read 1st time':
                atype = 'bill:reading:1'
            elif action == 'Read 2nd time':
                atype = 'bill:reading:2'
            elif action == 'Read 3rd time':
                atype = 'bill:reading:3'
            elif action == 'Filed for introduction':
                atype = 'bill:filed'
            elif action.startswith('To Governor') and 'Journal' not in action:
                atype = 'governor:received'
            elif re.match(r'To [A-Z]', action):
                atype = 'committee:referred'
            elif action.startswith('Introduced in'):
                atype = 'bill:introduced'
            elif (action.startswith('Approved by Governor') and
                  'Journal' not in action):
                atype = 'governor:signed'
            elif (action.startswith('Passed Senate') or
                  action.startswith('Passed House')):
                atype = 'bill:passed'
            elif (action.startswith('Reported do pass') or
                  action.startswith('With amendment, do pass')):
                atype = 'committee:passed'
            else:
                atype = 'other'

            bill.add_action(actor, action, date, type=atype)

        self.save_bill(bill)

    def scrape_vote(self, bill, url):
        filename, resp = self.urlretrieve(url)
        text = convert_pdf(filename, 'text')
        os.remove(filename)

        lines = text.splitlines()

        vote_type = None
        votes = collections.defaultdict(list)

        for idx, line in enumerate(lines):
            line = line.rstrip()
            match = re.search(r'(\d+)/(\d+)/(\d{4,4})$', line)
            if match:
                date = datetime.datetime.strptime(match.group(0), "%m/%d/%Y")
                continue

            match = re.match(
                r'\s+YEAS: (\d+)\s+NAYS: (\d+)\s+NOT VOTING: (\d+)',
                line)
            if match:
                motion = lines[idx - 2].strip()
                yes_count, no_count, other_count = [
                    int(g) for g  in match.groups()]

                exc_match = re.search(r'EXCUSED: (\d+)', line)
                if exc_match:
                    other_count += int(exc_match.group(1))

                if line.endswith('ADOPTED') or line.endswith('PASSED'):
                    passed = True
                else:
                    passed = False

                continue

            match = re.match(
                r'(YEAS|NAYS|NOT VOTING|PAIRED|EXCUSED):\s+(\d+)\s*$',
                line)
            if match:
                vote_type = {'YEAS': 'yes',
                             'NAYS': 'no',
                             'NOT VOTING': 'other',
                             'EXCUSED': 'other',
                             'PAIRED': 'paired'}[match.group(1)]
                continue

            if vote_type == 'paired':
                for part in line.split('   '):
                    part = part.strip()
                    if not part:
                        continue
                    name, pair_type = re.match(
                        r'([^\(]+)\((YEA|NAY)\)', line).groups()
                    name = name.strip()
                    if pair_type == 'YEA':
                        votes['yes'].append(name)
                    elif pair_type == 'NAY':
                        votes['no'].append(name)
            elif vote_type:
                for name in line.split('   '):
                    name = name.strip()
                    if not name:
                        continue
                    votes[vote_type].append(name)

        vote = Vote('lower', date, motion, passed,
                    yes_count, no_count, other_count)
        vote.add_source(url)

        vote['yes_votes'] = votes['yes']
        vote['no_votes'] = votes['no']
        vote['other_votes'] = votes['other']

        assert len(vote['yes_votes']) == yes_count
        assert len(vote['no_votes']) == no_count
        assert len(vote['other_votes']) == other_count

        bill.add_vote(vote)

    def _get_version_filenames(self, session, chamber):
        '''All bills have "versions", but for those lacking html documents,
        the .wpd file is available via ftp. Create a dict of those links
        in advance; any bills lacking html versions will get version info
        from this dict.'''

        try:
            f = open('/tmp/%s.wv_version_filenames.json' % chamber)
        except IOError:
            pass
        else:
            version_filenames = json.load(f)
            self._version_filenames = version_filenames
            return

        chamber_name = {'upper': 'senate', 'lower': 'House'}[chamber]
        ftp_url = 'ftp://www.legis.state.wv.us/publicdocs/%s/RS/%s/'
        ftp_url = ftp_url % (session, chamber_name)

        html = self.urlopen(ftp_url).decode('iso-8859-1')
        dirs = [' '.join(x.split()[3:]) for x in html.splitlines()]

        split = re.compile(r'\s+').split
        matchwpd = re.compile(r'\.wpd$', re.I).search
        splitext = os.path.splitext
        version_filenames = collections.defaultdict(list)
        for d in dirs:
            url = ('%s%s/' % (ftp_url, d)).replace(' ', '%20')
            html = self.urlopen(url).decode('iso-8859-1')
            filenames = [split(x, 3)[-1] for x in html.splitlines()]
            filenames = filter(matchwpd, filenames)

            for fn in filenames:
                fn, ext = splitext(fn)
                bill_id, _ = fn.split(' ', 1)
                version_filenames[bill_id.lower()].append((d, fn))

        self._version_filenames = version_filenames

        with open('/tmp/%s.wv_version_filenames.json' % chamber, 'w') as f:
            json.dump(version_filenames, f)

    def _scrape_versions_normally(self, session, chamber, page, bill_id,
                                  get_name=re.compile(r'\"(.+)"').search):
        '''This first method assumes the bills versions are hyperlinked
        on the bill's status page.
        '''
        for link in page.xpath("//a[starts-with(@title, 'HTML -')]"):
            # split name out of HTML - Introduced Version - SB 1
            name = link.get('title').split(' - ')[1]
            yield {'name': name, 'url': link.get('href'),
                   'mimetype': 'text/html'}

        for link in page.xpath("//a[contains(@title, 'WordPerfect')]"):
            name = get_name(link.get('title')).group(1)
            yield {'name': name, 'url': link.get('href'),
                   'mimetype': 'application/wordperfect'}

    def _scrape_versions_guess_url(self, session, chamber, page, bill_id):
        '''This second method uses the names listed on the legislature's
        ftp server to guess the link if it's not provided.
        '''

        _bill_id = bill_id.replace(' ', '').lower()
        billtype, i = bill_id.split()

        try:
            filenames = self._version_filenames[_bill_id]
        except KeyError:
            # There are no filenames in the dict for this bill_id.
            # Skip.
            return

        for folder, filename in filenames:

            _filename = filename.replace('+', '%20') + '.htm'

            urlparts = (
                'http',
                'www.legis.state.wv.us',
                '/Bill_Status/bills_text.cfm',
                '',
                urlencode([
                    ('billdoc', _filename),
                    ('yr', session),
                    ('sesstype', 'RS'),
                    ('i', i)]),
                '',)

            url = urlunparse(urlparts)
            yield filename, url

    def _scrape_versions_wpd(self, session, chamber, page, bill_id):
        '''This third method scrapes the .wpd document from the legislature's
        ftp server.
        '''
        chamber_name = {'upper': 'senate', 'lower': 'House'}[chamber]
        _bill_id = bill_id.replace(' ', '').lower()
        url = 'ftp://www.legis.state.wv.us/publicdocs/%s/RS' % session

        try:
            filenames = self._version_filenames[_bill_id]
        except KeyError:
            # There are no filenames in the dict for this bill_id.
            # Skip.
            return

        for folder, filename in filenames:
            _filename = quote(filename + '.wpd')
            url = '/'.join([url, chamber_name, folder, _filename])

            yield {'name': filename, 'url': url,
                   'mimetype': 'application/wordperfect'}

    def scrape_versions(self, session, chamber, page, bill_id):
        '''
        Return all available version documents for this bill_id.
        '''
        res = []
        cache = set()

        # Scrape .htm and .wpd versions listed in the detail page.
        for data in self._scrape_versions_normally(session, chamber, page,
                                                   bill_id):
            _url = _Url(data['url'])
            if _url not in cache:
                cache.add(_url)
                res.append(data)

        # For each guessed url not already scraped, add a version.
        not_available = ('let us know that the text for',
                         'is not available')
        for filename, url in self._scrape_versions_guess_url(
                                        session, chamber, page, bill_id):
            _url = _Url(url)
            if _url not in cache:
                try:
                    html = self.urlopen(url, retry_on_404=False).lower()
                except scrapelib.HTTPError:
                    # There wasn't a version document at the expected
                    # location.
                    continue
                else:
                    for s in not_available:
                        if s not in html:
                            continue

                    self.guess_count += 1
                    cache.add(_url)
                    data = {'url': url, 'name': filename,
                            'mimetype': 'text/html'}
                    res.append(data)

        # For each .wpd version not already scraped, add a version.
        for data in self._scrape_versions_wpd(session, chamber, page,
                                              bill_id):
            _url = _Url(data['url'])
            if _url not in cache:
                cache.add(_url)
                res.append(data)

        return res
