FROM postgres:16-alpine

RUN apk add --no-cache bash gzip tar coreutils findutils tzdata

COPY backup.sh restore.sh entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh /usr/local/bin/entrypoint.sh

WORKDIR /backup
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
