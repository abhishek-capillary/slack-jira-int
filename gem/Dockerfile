# Start with an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /usr/src/app

# Set environment variables (can be overridden by docker-compose or run command)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
# ENV HOST 0.0.0.0 # Already set in uvicorn command typically
# ENV PORT 3000    # Already set in uvicorn command typically

# Install system dependencies if any (e.g., for specific libraries)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY ./app ./app
COPY .env.example . # Good practice to have example, but actual .env should be mounted or variables injected

# Expose the port the app runs on
EXPOSE 3000

# Define the command to run the application
# Use Gunicorn with Uvicorn workers for production, or just Uvicorn for simplicity here
# For production, consider multiple workers:
# CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:3000", "app.main:app"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]