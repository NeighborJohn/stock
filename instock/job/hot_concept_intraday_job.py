#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import datetime as dt
import logging
import os.path
import sys
from typing import Any

import pandas as pd  # type: ignore[reportMissingImports]

cpath_current = os.path.dirname(os.path.dirname(__file__))
cpath = os.path.abspath(os.path.join(cpath_current, os.pardir))
sys.path.append(cpath)

import instock.core.hot_concept as hot_concept
import instock.core.stockfetch as stf
import instock.core.tablestructure as tbs
import instock.lib.database as mdb
from instock.lib.hot_concept_cli import (
    build_common_parser,
    setup_hot_concept_logging,
    should_skip_trade_date,
)

__author__ = 'myh '
__date__ = '2026/6/30 '


class HotConceptIntradayJobError(RuntimeError):
    pass


def _require_data(data: pd.DataFrame | None, source_name: str) -> pd.DataFrame:
    if data is None or data.empty:
        raise HotConceptIntradayJobError(f'missing-data: {source_name} is empty')
    return data


def _delete_partition(table_name: str, where: str, params: tuple[Any, ...]) -> None:
    if mdb.checkTableIsExist(table_name):
        mdb.executeSql(f'DELETE FROM `{table_name}` WHERE {where}', params)


def _insert_partition(
    data: pd.DataFrame,
    table: dict[str, Any],
    primary_keys: str,
    where: str,
    params: tuple[Any, ...],
) -> None:
    table_name = table['name']
    table_exists = mdb.checkTableIsExist(table_name)
    cols_type = None if table_exists else tbs.get_field_types(table['columns'])
    _delete_partition(table_name, where, params)
    mdb.insert_db_from_df(data, table_name, cols_type, False, primary_keys)
    inserted_count = mdb.executeSqlCount(f'SELECT COUNT(*) FROM `{table_name}` WHERE {where}', params)
    if inserted_count != len(data.index):
        raise HotConceptIntradayJobError(
            f'db-write-failed: {table_name} expected {len(data.index)} rows, found {inserted_count}'
        )


def _verify_db_connection() -> None:
    conn = mdb.get_connection()
    if conn is None:
        raise HotConceptIntradayJobError('db-connection-failed: database connection unavailable')
    conn.close()


def run_intraday_job(trade_date: dt.date, snapshot_time: str, config_path: str, top_n: int) -> None:
    config = hot_concept.load_score_config(config_path)
    digest = hot_concept.config_hash(config)
    logger = setup_hot_concept_logging(trade_date, snapshot_time, digest)

    if should_skip_trade_date(trade_date):
        message = 'non-trading day, skip'
        logger.info(message)
        print(message)
        return

    captured_at = dt.datetime.now().replace(microsecond=0)
    trade_date_value = trade_date.isoformat()

    selection_data = _require_data(stf.fetch_stock_selection(), 'stock selection')
    stock_data = _require_data(stf.fetch_stocks(trade_date), 'stock realtime snapshot')

    stock_snapshot = hot_concept.prepare_stock_snapshot(
        stock_data,
        trade_date_value,
        snapshot_time=snapshot_time,
        captured_at=captured_at,
        config_hash=digest,
    )
    membership = hot_concept.normalize_membership(
        selection_data,
        trade_date_value,
        snapshot_time=snapshot_time,
        captured_at=captured_at,
        membership_as_of_date=trade_date_value,
    )

    _require_data(stock_snapshot, 'prepared stock snapshot')
    _require_data(membership, 'normalized concept membership')

    aggregation = hot_concept.aggregate_concepts(
        stock_snapshot,
        membership,
        config,
        top_n=top_n,
        snapshot_time=snapshot_time,
        captured_at=captured_at,
        membership_as_of_date=trade_date_value,
    )
    concepts = _require_data(aggregation['concepts'], 'hot concept aggregates')
    top_stocks = _require_data(aggregation['top_stocks'], 'hot concept top stocks')

    _verify_db_connection()

    common_params = (trade_date_value, snapshot_time, digest)
    common_where = '`trade_date` = %s AND `snapshot_time` = %s AND `config_hash` = %s'
    _insert_partition(
        stock_snapshot,
        tbs.TABLE_CN_HOT_CONCEPT_STOCK_SNAPSHOT,
        '`trade_date`,`snapshot_time`,`code`,`config_hash`',
        common_where,
        common_params,
    )
    _insert_partition(
        membership,
        tbs.TABLE_CN_HOT_CONCEPT_MEMBERSHIP,
        '`trade_date`,`snapshot_time`,`concept_type`,`concept_name`,`code`',
        '`trade_date` = %s AND `snapshot_time` = %s',
        (trade_date_value, snapshot_time),
    )
    _insert_partition(
        concepts,
        tbs.TABLE_CN_HOT_CONCEPT_SNAPSHOT,
        '`trade_date`,`snapshot_time`,`concept_type`,`concept_name`,`config_hash`',
        common_where,
        common_params,
    )
    _insert_partition(
        top_stocks,
        tbs.TABLE_CN_HOT_CONCEPT_TOP_STOCK,
        '`trade_date`,`snapshot_time`,`concept_type`,`concept_name`,`rank`,`config_hash`',
        common_where,
        common_params,
    )

    diagnostics = aggregation.get('diagnostics', {})
    logger.info(
        'intraday hot concept capture complete: stocks=%s memberships=%s concepts=%s top_stocks=%s diagnostics=%s',
        len(stock_snapshot.index),
        len(membership.index),
        len(concepts.index),
        len(top_stocks.index),
        diagnostics,
    )
    print(
        f'intraday hot concept capture complete: stocks={len(stock_snapshot.index)} '
        f'memberships={len(membership.index)} concepts={len(concepts.index)} '
        f'top_stocks={len(top_stocks.index)} config_hash={digest}'
    )


def main() -> None:
    parser = build_common_parser('Capture and aggregate an intraday hot concept snapshot.')
    args = parser.parse_args()
    try:
        run_intraday_job(args.trade_date, args.snapshot_time, str(args.config), args.top_n)
    except HotConceptIntradayJobError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        logging.exception('hot concept intraday job failed')
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
