"""Query OSV for installed distributions; sends only public names and versions."""
import importlib.metadata
import json
import urllib.request


def query(path, payload):
    request = urllib.request.Request('https://api.osv.dev/v1/' + path,
        data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main():
    packages = sorted({(d.metadata['Name'], d.version) for d in importlib.metadata.distributions()
                       if d.metadata.get('Name') and d.metadata['Name'].lower().replace('_','-') != 'meta-ads-mcp'})
    queries = [{'package':{'name':name,'ecosystem':'PyPI'},'version':version} for name,version in packages]
    findings = []
    for start in range(0, len(queries), 100):
        batch = queries[start:start+100]
        results = query('querybatch', {'queries':batch})['results']
        if len(results) != len(batch):
            raise RuntimeError('Incomplete OSV response')
        for item, result in zip(batch, results):
            ids = {v['id'] for v in result.get('vulns',[])}
            seen = set()
            while result.get('next_page_token'):
                cursor = result['next_page_token']
                if cursor in seen:
                    raise RuntimeError('Repeated OSV pagination cursor')
                seen.add(cursor)
                result = query('query', {**item, 'page_token':cursor})
                ids.update(v['id'] for v in result.get('vulns',[]))
            if ids:
                findings.append({**item, 'advisories':sorted(ids)})
    print(json.dumps({'packages_checked':len(packages),'findings':findings},indent=2))
    return int(bool(findings))


if __name__ == '__main__':
    raise SystemExit(main())
