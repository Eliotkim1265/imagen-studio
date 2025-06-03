# Dockerfile
# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install system dependencies (if any, e.g., for Pillow)
# RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev gcc

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy project code
COPY . /app/

# Expose port (Cloud Run will set this via PORT env var, but good practice)
EXPOSE 8000

# Collect static files (optional, if you serve static files via Django/Whitenoise in the container)
# If using GCS for static files, this might not be needed here.
RUN python manage.py collectstatic --noinput
RUN python manage.py migrate --noinput
RUN python manage.py createsuperuser
# Default command to run the app using Gunicorn (recommended for production)
# Cloud Run sets the PORT environment variable.
CMD exec gunicorn media_studio_project.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --worker-class gthread