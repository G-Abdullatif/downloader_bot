# Use an official Python 3.11 slim image as a base
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Update package lists and install ffmpeg
# This command is run during the image build, where permissions are granted
RUN apt-get update && apt-get install -y ffmpeg --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy your bot's code into the container
COPY . .

# The command to run your bot when the container starts
CMD ["python3", "bot.py"]