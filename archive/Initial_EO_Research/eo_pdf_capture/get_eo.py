import os
import requests

# Create folder if it doesn't exist
output_folder = "EO_PDFs"
os.makedirs(output_folder, exist_ok=True)

# Path to your input file
input_file = "URLs.txt"

# Read and process each URL
with open(input_file, "r") as file:
    urls = [line.strip() for line in file if line.strip()]

for url in urls:
    try:
        filename = os.path.basename(url)
        output_path = os.path.join(output_folder, filename)

        print(f"Downloading: {url}")
        response = requests.get(url)
        response.raise_for_status()  # Raise error for bad status

        with open(output_path, "wb") as f:
            f.write(response.content)
        print(f"Saved to: {output_path}")
    except Exception as e:
        print(f"Failed to download {url}: {e}")