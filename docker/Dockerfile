FROM python:3.12-slim
# MailFilter — auth deps (shared-auth: bcrypt + JWT HS256, stejné jako CDprocesy)
RUN pip install --no-cache-dir bcrypt PyJWT
WORKDIR /app
CMD ["python3", "/app/server.py"]
