# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy requirements if available, else install manually
COPY requirements.txt ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt || \
    pip install --no-cache-dir requests PyPubSub meshtastic

# Copy the rest of the application code
COPY . .

# Set environment variables (optional, for Docker best practices)
ENV PYTHONUNBUFFERED=1

# Default command to run your application
CMD ["python", "main.py"]
