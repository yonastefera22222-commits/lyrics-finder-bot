FROM python:3.11-slim

# Install ffmpeg and any other needed system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Expose the port for Flask (Render/Heroku/etc. will set $PORT)
EXPOSE 8080

# Start your bot
CMD ["python", "main.py"]