# Edge Inference at Scale — Backend Service Container
# UBI9-based for Red Hat ecosystem compatibility

FROM registry.access.redhat.com/ubi9/ubi-minimal:9.4

RUN microdnf install -y python3.11 python3.11-pip && \
    microdnf clean all

WORKDIR /app

COPY requirements.txt .
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

COPY . /app/backend/
ENV PYTHONPATH=/app

USER 1001

EXPOSE 8000
CMD ["python3.11", "-m", "uvicorn", "backend.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
