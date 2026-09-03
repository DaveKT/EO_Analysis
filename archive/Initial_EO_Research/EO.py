from bs4 import BeautifulSoup
import csv
from datetime import datetime

# Load the HTML file
with open("/mnt/data/extract.txt", "r", encoding="utf-8") as file:
    soup = BeautifulSoup(file, "html.parser")

# Output headers
headers = ["EO #", "Title", "URL", "Date Signed", "Date Published", "FR Citation", "FR Doc Number"]
rows = []

# Parse each executive order block
for block in soup.select("div.presidential-document-wrapper"):
    eo_number_tag = block.select_one(".eo-number h5")
    eo_number = eo_number_tag.text.strip().replace(":", "") if eo_number_tag else ""

    # Extract Title and relative URL, convert to absolute
    title_tag = block.select_one("div.col-md-10 > h5 > a")
    title = title_tag.text.strip() if title_tag else ""
    url = title_tag["href"] if title_tag and "href" in title_tag.attrs else ""
    if url and not url.startswith("http"):
        url = "https://www.federalregister.gov" + url

    # Extract metadata fields
    metadata = {
        "Signed:": "",
        "Published:": "",
        "FR Citation:": "",
        "FR Doc. Number:": ""
    }

    for dt, dd in zip(block.select("dt"), block.select("dd")):
        label = dt.text.strip()
        if label in metadata:
            metadata[label] = dd.text.strip()

    # Format dates as YYYY-MM-DD
    def format_date(d):
        try:
            return datetime.strptime(d, "%B %d, %Y").strftime("%Y-%m-%d")
        except:
            return d

    row = [
        eo_number,
        title,
        url,
        format_date(metadata["Signed:"]),
        format_date(metadata["Published:"]),
        metadata["FR Citation:"],
        metadata["FR Doc. Number:"]
    ]
    rows.append(row)

# Write to CSV
output_csv = "/mnt/data/federal_register_eos.csv"
with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(headers)
    writer.writerows(rows)