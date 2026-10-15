FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data DISPLAY=:99
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN /usr/bin/python3 -m virtualenv --no-download /opt/venv && python -m pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY tests ./tests
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 755 /app/entrypoint.sh && mkdir -p /data && chown -R pwuser:pwuser /data /app
USER pwuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
ENTRYPOINT ["/app/entrypoint.sh"]
