#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os.path
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[reportMissingImports]

cpath_current = os.path.dirname(os.path.dirname(__file__))
cpath = os.path.abspath(os.path.join(cpath_current, os.pardir))
sys.path.append(cpath)

import instock.core.hot_concept as hot_concept
import instock.core.tablestructure as tbs
import instock.lib.database as mdb
import instock.lib.trade_time as trade_time
from instock.lib.hot_concept_cli import parse_trade_date, positive_int

__author__ = 'myh '
__date__ = '2026/6/30 '


class HotConceptHistoryJobError(RuntimeError):
    pass


@dataclass
class MissingDataReport:
    skipped_non_trading_dates: list[str] = field(default_factory=list)
    missing_stock_dates: list[str] = field(default_factory=list)
    missing_membership_dates: list[str] = field(default_factory=list)
    processed_trade_dates: list[str] = field(default_factory=list)
    db_credential_blocker: str | None = None

    def lines(self) -> list[str]:
        return [
            'missing-data report:',
            f'  processed_trade_dates: {self.processed_trade_dates}',
            f'  skipped_non_trading_dates: {self.skipped_non_trading_dates}',
            f'  missing_stock_dates: {self.missing_stock_dates}',
            f'  missing_membership_dates: {self.missing_membership_dates}',
            f'  db_credential_blocker: {self.db_credential_blocker}',
        ]


def build_history_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Aggregate historical hot concepts for a trading-day date range.',
        allow_abbrev=False,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--start-date', required=True, type=parse_trade_date, metavar='YYYY-MM-DD')
    parser.add_argument('--end-date', required=True, type=parse_trade_date, metavar='YYYY-MM-DD')
    parser.add_argument('--config', required=True, type=Path, metavar='PATH')
    parser.add_argument('--top-n', default=20, type=positive_int, metavar='INT')
    return parser


def _date_range(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    if start_date > end_date:
        raise HotConceptHistoryJobError('invalid-date-range: --start-date must be on or before --end-date')
    day_count = (end_date - start_date).days + 1
    return [start_date + dt.timedelta(days=offset) for offset in range(day_count)]


def _print_report(report: MissingDataReport) -> None:
    for line in report.lines():
        print(line)


def _verify_db_connection(report: MissingDataReport) -> None:
    try:
        conn = mdb.get_connection()
    except Exception as exc:
        report.db_credential_blocker = str(exc)
        raise HotConceptHistoryJobError(f'db-credential-blocker: {exc}') from exc
    if conn is None:
        report.db_credential_blocker = 'database connection unavailable'
        raise HotConceptHistoryJobError('db-credential-blocker: database connection unavailable')
    conn.close()


def _read_table(sql: str, params: tuple[Any, ...]) -> pd.DataFrame:
    return pd.read_sql(sql=sql, con=mdb.engine(), params=params)


def _table_exists(table_name: str) -> bool:
    return bool(mdb.checkTableIsExist(table_name))


def _load_stock_rows(trade_date: dt.date) -> pd.DataFrame:
    table_name = tbs.TABLE_CN_STOCK_SPOT['name']
    return _read_table(f'SELECT * FROM `{table_name}` WHERE `date` = %s', (trade_date,))


def _load_hot_concept_membership(trade_date: dt.date, captured_at: dt.datetime) -> tuple[pd.DataFrame, dt.date | None]:
    table_name = tbs.TABLE_CN_HOT_CONCEPT_MEMBERSHIP['name']
    if not _table_exists(table_name):
        return pd.DataFrame(), None

    as_of_rows = _read_table(
        f'SELECT MAX(`membership_as_of_date`) AS membership_as_of_date FROM `{table_name}` '
        'WHERE `membership_as_of_date` <= %s',
        (trade_date,),
    )
    if as_of_rows.empty or pd.isna(as_of_rows.iloc[0]['membership_as_of_date']):
        return pd.DataFrame(), None

    as_of_date = pd.to_datetime(as_of_rows.iloc[0]['membership_as_of_date']).date()
    membership = _read_table(
        f'SELECT * FROM `{table_name}` WHERE `membership_as_of_date` = %s',
        (as_of_date,),
    )
    if membership.empty:
        return membership, as_of_date

    membership = membership.copy()
    membership['trade_date'] = trade_date.isoformat()
    membership['snapshot_time'] = None
    membership['captured_at'] = captured_at
    membership['membership_as_of_date'] = as_of_date.isoformat()
    membership = membership.drop_duplicates(['concept_type', 'concept_name', 'code']).reset_index(drop=True)
    return membership, as_of_date


def _load_selection_membership(trade_date: dt.date, captured_at: dt.datetime) -> tuple[pd.DataFrame, dt.date | None]:
    table_name = tbs.TABLE_CN_STOCK_SELECTION['name']
    if not _table_exists(table_name):
        return pd.DataFrame(), None

    as_of_rows = _read_table(
        f'SELECT MAX(`date`) AS selection_date FROM `{table_name}` WHERE `date` <= %s',
        (trade_date,),
    )
    if as_of_rows.empty or pd.isna(as_of_rows.iloc[0]['selection_date']):
        return pd.DataFrame(), None

    as_of_date = pd.to_datetime(as_of_rows.iloc[0]['selection_date']).date()
    selection = _read_table(f'SELECT * FROM `{table_name}` WHERE `date` = %s', (as_of_date,))
    membership = hot_concept.normalize_membership(
        selection,
        trade_date.isoformat(),
        snapshot_time=None,
        captured_at=captured_at,
        membership_as_of_date=as_of_date.isoformat(),
    )
    return membership, as_of_date


def _load_membership(trade_date: dt.date, captured_at: dt.datetime) -> tuple[pd.DataFrame, dt.date | None, str | None]:
    membership, as_of_date = _load_hot_concept_membership(trade_date, captured_at)
    if not membership.empty:
        return membership, as_of_date, 'cn_hot_concept_membership'
    membership, as_of_date = _load_selection_membership(trade_date, captured_at)
    if not membership.empty:
        return membership, as_of_date, 'cn_stock_selection'
    return membership, as_of_date, None


def _delete_partition(table_name: str, where: str, params: tuple[Any, ...]) -> None:
    if _table_exists(table_name):
        mdb.executeSql(f'DELETE FROM `{table_name}` WHERE {where}', params)


def _insert_partition(
    data: pd.DataFrame,
    table: dict[str, Any],
    primary_keys: str,
    where: str,
    params: tuple[Any, ...],
) -> None:
    table_name = table['name']
    table_exists = _table_exists(table_name)
    cols_type = None if table_exists else tbs.get_field_types(table['columns'])
    _delete_partition(table_name, where, params)
    mdb.insert_db_from_df(data, table_name, cols_type, False, primary_keys)
    inserted_count = mdb.executeSqlCount(f'SELECT COUNT(*) FROM `{table_name}` WHERE {where}', params)
    if inserted_count != len(data.index):
        raise HotConceptHistoryJobError(
            f'db-write-failed: {table_name} expected {len(data.index)} rows, found {inserted_count}'
        )


def _history_concepts(concepts: pd.DataFrame) -> pd.DataFrame:
    return concepts.drop(columns=['snapshot_time'], errors='ignore')


def _history_top_stocks(top_stocks: pd.DataFrame) -> pd.DataFrame:
    return top_stocks.drop(columns=['snapshot_time'], errors='ignore')


def run_history_job(start_date: dt.date, end_date: dt.date, config_path: str, top_n: int) -> MissingDataReport:
    report = MissingDataReport()
    calendar_dates = _date_range(start_date, end_date)
    config = hot_concept.load_score_config(config_path)
    digest = hot_concept.config_hash(config)
    captured_at = dt.datetime.now().replace(microsecond=0)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s hot_concept_history %(message)s', force=True)

    trade_dates: list[dt.date] = []
    for calendar_date in calendar_dates:
        if trade_time.is_trade_date(calendar_date):
            trade_dates.append(calendar_date)
        else:
            report.skipped_non_trading_dates.append(calendar_date.isoformat())

    if not trade_dates:
        _print_report(report)
        return report

    try:
        _verify_db_connection(report)
    except HotConceptHistoryJobError:
        _print_report(report)
        raise

    for calendar_date in trade_dates:
        trade_date_value = calendar_date.isoformat()
        stock_data = _load_stock_rows(calendar_date)
        if stock_data.empty:
            report.missing_stock_dates.append(trade_date_value)
            continue

        membership, membership_as_of_date, membership_source = _load_membership(calendar_date, captured_at)
        if membership.empty or membership_as_of_date is None:
            report.missing_membership_dates.append(trade_date_value)
            continue

        stock_snapshot = hot_concept.prepare_stock_snapshot(
            stock_data,
            trade_date_value,
            snapshot_time=None,
            captured_at=captured_at,
            config_hash=digest,
        )
        if stock_snapshot.empty:
            report.missing_stock_dates.append(trade_date_value)
            continue

        aggregation = hot_concept.aggregate_concepts(
            stock_snapshot,
            membership,
            config,
            top_n=top_n,
            snapshot_time=None,
            captured_at=captured_at,
            membership_as_of_date=membership_as_of_date.isoformat(),
        )
        concepts = _history_concepts(aggregation['concepts'])
        top_stocks = _history_top_stocks(aggregation['top_stocks'])
        if concepts.empty or top_stocks.empty:
            report.missing_membership_dates.append(trade_date_value)
            continue

        common_where = '`trade_date` = %s AND `config_hash` = %s'
        common_params = (trade_date_value, digest)
        _insert_partition(
            concepts,
            tbs.TABLE_CN_HOT_CONCEPT_HISTORY,
            '`trade_date`,`concept_type`,`concept_name`,`config_hash`',
            common_where,
            common_params,
        )
        _insert_partition(
            top_stocks,
            tbs.TABLE_CN_HOT_CONCEPT_HISTORY_TOP_STOCK,
            '`trade_date`,`concept_type`,`concept_name`,`rank`,`config_hash`',
            common_where,
            common_params,
        )
        report.processed_trade_dates.append(trade_date_value)
        logging.info(
            'history aggregation complete: trade_date=%s stocks=%s memberships=%s concepts=%s top_stocks=%s membership_as_of_date=%s membership_source=%s config_hash=%s diagnostics=%s',
            trade_date_value,
            len(stock_snapshot.index),
            len(membership.index),
            len(concepts.index),
            len(top_stocks.index),
            membership_as_of_date.isoformat(),
            membership_source,
            digest,
            aggregation.get('diagnostics', {}),
        )

    _print_report(report)
    return report


def main() -> None:
    parser = build_history_parser()
    args = parser.parse_args()
    try:
        report = run_history_job(args.start_date, args.end_date, str(args.config), args.top_n)
        if not report.processed_trade_dates and (report.missing_stock_dates or report.missing_membership_dates):
            raise HotConceptHistoryJobError('missing-data: no trade dates were processed')
    except HotConceptHistoryJobError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        logging.exception('hot concept history job failed')
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
