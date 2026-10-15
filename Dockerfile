FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
COPY public /app/public
RUN useradd --system --uid 10001 --create-home sozmarzhan && mkdir -p /app/data && chown -R sozmarzhan:sozmarzhan /app
USER sozmarzhan
ENV SOZMARZHAN_DATA_DIR=/app/data PORT=8080
EXPOSE 8080
VOLUME ["/app/data"]
CMD ["python", "server.py"]
