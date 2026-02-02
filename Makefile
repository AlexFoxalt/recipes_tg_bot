docker_build:
	docker build -t recipes-tg-bot .

docker_start:
	docker compose up -d --build

docker_stop:
	docker compose down
