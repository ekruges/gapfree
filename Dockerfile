FROM python:3.12-alpine
RUN apk add --no-cache git
WORKDIR /app
COPY gapfree.py .
ENV GAPFREE_HOME=/data GAPFREE_BIND=0.0.0.0 GAPFREE_PORT=7331
VOLUME /data
EXPOSE 7331
CMD ["python3", "gapfree.py", "serve"]
