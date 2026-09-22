"""verify-aliases: historical titles proven by single-company exports (mocked transport)."""
import json

import httpx


from stock_crawler.crawl.aliases import candidate_tickers_from_summary, verify_aliases
from stock_crawler.crawl.fetch import PacedClient
from stock_crawler.crawl.kap_export import CompanyRegistry, KapExportClient
from stock_crawler.crawl.cli import main
from stock_crawler.crawl.pipeline import Pipeline, SyncOptions
from stock_crawler.core.storage import RawStore, StateStore
from .test_kap_export import REGISTRY, modified_book

OLD_THYAO_TITLE = 'TÜRK HAVA YOLLARI ANONİM ORTAKLIĞI (ESKİ UNVAN)'


def _rename_thyao(sheet):
    # The batch response carries THYAO's rows under a title the registry has never seen.
    for number in (10, 11):
        sheet.cell(number, 1, OLD_THYAO_TITLE)


def _renamed_book():
    return modified_book(_rename_thyao)


def _single_company_book(rows_for):
    """Rows for one company only, as a one-ID request would return (with THYAO's old title)."""
    def change(sheet):
        _rename_thyao(sheet)
        for number in range(sheet.max_row, 7, -1):
            if sheet.cell(number, 1).value not in rows_for:
                sheet.delete_rows(number)
    return modified_book(change)


def _setup(settings, clock, responder, requests):
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=responder(requests[-1]),
                              headers={'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
    settings.kap_company_registry = REGISTRY
    settings.kap_alias_file = settings.data_dir / 'kap_aliases.json'
    settings.kap_years = [2023, 2024]
    state = StateStore(settings.data_dir, clock=clock)
    fetcher = PacedClient(httpx.Client(transport=httpx.MockTransport(respond)), settings, state, allowed_hosts={'www.kap.org.tr'}, clock=clock)
    return state, fetcher


def test_rejected_title_is_proven_by_a_one_company_request_and_the_company_becomes_due(settings, repo, clock):
    requests = []
    renamed = _renamed_book()
    thyao_id = CompanyRegistry(REGISTRY).resolve('THYAO').source_company_id

    def responder(payload):
        if payload['mkkMemberIdList'] == [thyao_id]:
            return _single_company_book({OLD_THYAO_TITLE})
        return renamed

    state, fetcher = _setup(settings, clock, responder, requests)
    source = KapExportClient(fetcher, settings, clock=clock)
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock)
    first = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO']))
    assert first.data['rejected_row_count'] == 2
    assert {(r['ticker'], r['status']) for r in first.data['companies'] if r['ticker'] == 'THYAO'} == {('THYAO', 'no_filing')}

    registry = CompanyRegistry(settings.kap_company_registry, settings.kap_alias_file)
    candidates = candidate_tickers_from_summary(first.data, registry)
    assert candidates == ['THYAO'], 'ASELS had rows for both years, so only THYAO can own the rejected title'

    outcome = verify_aliases(candidates, settings=settings, fetcher=fetcher, registry=registry,
                             raw=RawStore(settings.data_dir), state=state, alias_path=settings.kap_alias_file, clock=clock)
    assert [a['ticker'] for a in outcome.new_aliases] == ['THYAO'] and outcome.new_aliases[0]['title'] == OLD_THYAO_TITLE
    assert requests[-1]['mkkMemberIdList'] == [thyao_id] and requests[-1]['yearList'] == ['2023', '2024']
    evidence = outcome.new_aliases[0]['evidence']
    assert evidence['method'] == 'single_company_export' and len(evidence['notifications']) == 2
    assert not outcome.conflicts

    # Same client, same run: THYAO is due again and now publishes under the proven title.
    source = KapExportClient(fetcher, settings, clock=clock)  # reloads the registry with the alias file
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock)
    second = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO']))
    assert second.data['rejected_row_count'] == 0
    assert {(r['ticker'], r['status']) for r in second.data['companies']} == {('ASELS', 'fresh'), ('THYAO', 'published')}
    assert len(requests) == 3
    fetcher.close()


def test_title_owned_by_another_ticker_is_a_conflict_not_an_alias(settings, clock):
    requests = []
    state, fetcher = _setup(settings, clock, lambda payload: _single_company_book({'ASELSAN ELEKTRONİK SANAYİ VE TİCARET A.Ş.'}), requests)
    registry = CompanyRegistry(REGISTRY)
    outcome = verify_aliases(['THYAO'], settings=settings, fetcher=fetcher, registry=registry,
                             raw=RawStore(settings.data_dir), state=state, alias_path=settings.kap_alias_file, clock=clock)
    assert not outcome.new_aliases
    assert outcome.conflicts and outcome.conflicts[0]['registry_ticker'] == 'ASELS'
    assert json.loads(settings.kap_alias_file.read_text())['aliases'] == []
    fetcher.close()


def test_empty_answer_is_recorded_and_request_cap_is_respected(settings, clock):
    requests = []
    state, fetcher = _setup(settings, clock, lambda payload: _single_company_book(set()), requests)
    registry = CompanyRegistry(REGISTRY)
    outcome = verify_aliases(['ASELS', 'THYAO', 'BIMAS'], settings=settings, fetcher=fetcher, registry=registry,
                             raw=RawStore(settings.data_dir), state=state, alias_path=settings.kap_alias_file,
                             max_requests=2, clock=clock)
    assert [r['status'] for r in outcome.checked] == ['no_rows', 'no_rows'] and len(requests) == 2
    assert outcome.stopped_reason and 'cap of 2' in outcome.stopped_reason
    document = json.loads(settings.kap_alias_file.read_text())
    assert [e['ticker'] for e in document['verified_empty']] == ['ASELS', 'THYAO']
    fetcher.close()


def test_cli_reports_nothing_to_verify_when_every_title_resolves(tmp_path, capsys):
    data_dir = tmp_path / 'data'
    (data_dir / 'runs' / 'r1').mkdir(parents=True)
    (data_dir / 'runs' / 'r1' / 'summary.json').write_text(json.dumps({
        'command': 'sync', 'started_at': '2026-09-19T11:08:56+00:00', 'companies': [], 'rejected_rows': []}))
    env = tmp_path / '.env'
    env.write_text(f"SOURCE_MODE=kap-export\nDATA_DIR={data_dir}\nKAP_COMPANY_REGISTRY={REGISTRY}\nMSSQL_PASSWORD=x\n")
    assert main(['--env-file', str(env), 'verify-aliases']) == 0
    assert 'nothing to verify' in capsys.readouterr().out
