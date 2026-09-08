FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install uv
RUN pip install --upgrade pip && \
    pip install uv

# Copy requirements file
COPY requirements.txt .

# Install dependencies using uv with --system flag
RUN uv pip install --system -r requirements.txt

# Only application source belongs in the runtime image.
COPY meta_ads_mcp ./meta_ads_mcp
RUN useradd --create-home --uid 10001 app
USER app

# Command to run the Meta Ads MCP server
CMD ["python", "-m", "meta_ads_mcp"]
