# Headless rigs: keep the monitor running on a server/NAS.
#
#   docker build -t ed-afk-monitor .
#   docker run --rm -it \
#     -v "<path-to-Frontier-journals>:/journal:ro" \
#     -v ed-afk-config:/app \
#     ed-afk-monitor
#
# The named volume keeps config, ledgers, history and outbox across container
# rebuilds; on first run copy afk_monitor.example.toml to afk_monitor.toml
# inside the volume (docker run ... sh -c 'cp ...; python afk_monitor.py ...').
# Journals are mounted read-only; Frontier writes them itself.
FROM python:3.12-slim
WORKDIR /app
COPY afk_monitor.py journal_reader.py missions.py notifications.py outbox.py \
     session_store.py session_card.py ship_pools.py journal_schema.py \
     config_overrides.py terminal_output.py dashboard.py ships.json \
     afk_monitor.example.toml README.md LICENSE ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "afk_monitor.py", "--plain"]
