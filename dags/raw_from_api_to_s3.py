"""DAG: raw_from_api_to_s3

Этот модуль содержит Airflow DAG, который выполняет две основные задачи:
1. Запрашивает данные о землетрясениях у публичного USGS API за интервал (data_interval) и
   читает их как CSV.
2. Сохраняет полученные raw-данные в MinIO (совместимое S3) в формате Parquet через DuckDB.

Файл организован так, чтобы работать в окружении Docker Compose (в сети `minio`) и использовать
Airflow Variable для хранения доступа к S3 (`access_key`, `secret_key`).
"""

import logging

import duckdb
import pendulum
from airflow import DAG
from airflow.models import Variable
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import PythonOperator

# --- Конфигурация DAG и метаданные ---
OWNER = "almas.maksutbekov"  # автор/владелец DAG
DAG_ID = "raw_from_api_to_s3"  # идентификатор DAG в Airflow

# Логическая организация данных: слой и источник
LAYER = "raw"  # слой хранения в S3
SOURCE = "earthquake"  # источник данных

# S3 (MinIO) — получаем креды из Airflow Variables
ACCESS_KEY = Variable.get("access_key")
SECRET_KEY = Variable.get("secret_key")

# Описания (можно использовать в UI Airflow)
LONG_DESCRIPTION = """
# Заглавное описание DAG (можно расширить при необходимости)
"""

SHORT_DESCRIPTION = "Загрузка raw данных из USGS в S3 (MinIO) через DuckDB"

args = {
    "owner": OWNER,
    "start_date": pendulum.datetime(2025, 5, 1, tz="Europe/Moscow"),
    "catchup": True,
    "retries": 3,
    "retry_delay": pendulum.duration(hours=1),
}


def get_dates(**context) -> tuple[str, str]:
    """Получить границы интервала выполнения DAG из контекста Airflow.

    Airflow передаёт `data_interval_start` и `data_interval_end` в объекте `context`.
    Функция форматирует их в строку YYYY-MM-DD и возвращает кортеж (start_date, end_date).
    """
    start_date = context["data_interval_start"].format("YYYY-MM-DD")
    end_date = context["data_interval_end"].format("YYYY-MM-DD")

    return start_date, end_date


def get_and_transfer_api_data_to_s3(**context):
    """Скачать CSV из USGS API и положить в S3 (MinIO) в формате Parquet.

    Шаги:
    - вычислить `start_date` и `end_date` для запроса из `get_dates`
    - подключиться к DuckDB (в памяти)
    - установить и загрузить плагин `httpfs` для работы с S3
    - настроить параметры подключения к MinIO (endpoint, креды, стиль URL)
    - считать CSV напрямую из публичного HTTP-адреса USGS через `read_csv_auto`
    - выполнить `COPY ... TO 's3://.../...parquet'` — результат будет записан в MinIO

    Примечание: запись идёт в путь вида
      s3://prod/{LAYER}/{SOURCE}/{start_date}/{start_date}_00-00-00.gz.parquet
    Это можно изменить под ваши требования к именованию.
    """

    start_date, end_date = get_dates(**context)
    logging.info(f"💻 Start load for dates: {start_date}/{end_date}")

    # Подключаемся к локальному инстансу DuckDB (в памяти)
    con = duckdb.connect()

    # Запрос к DuckDB: настраиваем httpfs и s3, затем читаем CSV по HTTP и копируем в S3 как parquet
    con.sql(
        f"""--sql
        SET TIMEZONE='UTC';
        INSTALL httpfs;
        LOAD httpfs;
        SET s3_url_style = 'path';
        SET s3_endpoint = 'minio:9000';
        SET s3_access_key_id = '{ACCESS_KEY}';
        SET s3_secret_access_key = '{SECRET_KEY}';
        SET s3_use_ssl = FALSE;

        COPY
        (
            SELECT
                *
            FROM
                read_csv_auto('https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&starttime={start_date}&endtime={end_date}') AS res
        ) TO 's3://prod/{LAYER}/{SOURCE}/{start_date}/{start_date}_00-00-00.gz.parquet';

        """,
    )

    con.close()
    logging.info(f"✅ Download for date success: {start_date}")


with DAG(
    dag_id=DAG_ID,
    schedule_interval="0 5 * * *",  # запуск ежедневно в 05:00
    default_args=args,
    tags=["s3", "raw"],
    description=SHORT_DESCRIPTION,
    concurrency=1,
    max_active_tasks=1,
    max_active_runs=1,
) as dag:
    # Описание DAG в UI
    dag.doc_md = LONG_DESCRIPTION

    # Простая структура: старт -> загрузка -> конец
    start = EmptyOperator(
        task_id="start",
    )

    # PythonOperator вызывает определённую выше функцию
    get_and_transfer_api_data_to_s3 = PythonOperator(
        task_id="get_and_transfer_api_data_to_s3",
        python_callable=get_and_transfer_api_data_to_s3,
    )

    end = EmptyOperator(
        task_id="end",
    )

    start >> get_and_transfer_api_data_to_s3 >> end