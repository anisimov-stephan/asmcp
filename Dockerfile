# FROM registry.auto.local:5000/gitlab-runner/totally-nonexistent-frontend-replace-me:latest as static_frontend

FROM registry.auto.local:5000/gitlab-runner/uv-pylint:latest

RUN mkdir -p /var/www

# COPY --from=static_frontend /var/www /var/www

WORKDIR /app
COPY . .

# RUN uv sync --frozen
RUN uv sync

EXPOSE 8000

CMD ["uv", "run", "--active", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
