# Dockerfile for Trino MCP Server with Jupyter Chat Interface
FROM python:3.11-slim

# Proxy configuration (override with build args if needed)
ARG http_proxy
ARG https_proxy
ARG no_proxy=localhost,127.0.0.1

ENV HTTP_PROXY=${http_proxy}
ENV HTTPS_PROXY=${https_proxy}
ENV NO_PROXY=${no_proxy}
ENV http_proxy=${http_proxy}
ENV https_proxy=${https_proxy}
ENV no_proxy=${no_proxy}

# If proxy is set, disable SSL verification for MITM proxies
RUN if [ -n "$http_proxy" ] || [ -n "$HTTP_PROXY" ]; then \
        echo "Proxy detected - configuring SSL bypass"; \
        mkdir -p /root/.pip && \
        echo "[global]" > /root/.pip/pip.conf && \
        echo "trusted-host = pypi.org" >> /root/.pip/pip.conf && \
        echo "               pypi.python.org" >> /root/.pip/pip.conf && \
        echo "               files.pythonhosted.org" >> /root/.pip/pip.conf; \
    fi
ENV GIT_SSL_NO_VERIFY=${http_proxy:+true}
ENV PYTHONHTTPSVERIFY=${http_proxy:+0}

# Install system dependencies
RUN apt-get update && apt-get install -y \
        curl \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configure git proxy if set
RUN if [ -n "$http_proxy" ] || [ -n "$HTTP_PROXY" ]; then \
        git config --global http.sslVerify false && \
        git config --global http.proxy "${http_proxy:-$HTTP_PROXY}"; \
    fi

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY src/ ./src/
COPY README.md .

# Install project dependencies
RUN pip install --no-cache-dir --prefer-binary -e .

# Install Jupyter and chat interface dependencies
RUN pip install --no-cache-dir --prefer-binary \
    jupyter \
    notebook \
    ipywidgets \
    openai \
    requests \
    python-dotenv

# Expose Jupyter port
EXPOSE 8888

# Create directory for notebooks
RUN mkdir -p /app/notebooks

# Copy the chat notebook
COPY notebooks/ ./notebooks/

# Set environment variables for Jupyter
ENV JUPYTER_ENABLE_LAB=yes

# Start Jupyter notebook
CMD ["jupyter", "notebook", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--NotebookApp.token=''", "--NotebookApp.password=''"]
