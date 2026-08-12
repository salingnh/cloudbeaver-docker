IMAGE ?= cloudbeaver-full:local

.PHONY: build up down logs report shell clean

build:
	docker build --progress=plain -t $(IMAGE) .

up:
	CLOUDBEAVER_IMAGE=$(IMAGE) docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f cloudbeaver

report:
	docker exec cloudbeaver-full cat /opt/cloudbeaver/full-drivers-report.json

shell:
	docker exec -it cloudbeaver-full bash

clean:
	docker compose down -v --remove-orphans
