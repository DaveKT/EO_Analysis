import pandas as pd
import requests
from bs4 import BeautifulSoup
import csv
import openai
import os

# Set your OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")  # Or replace with your actual key for local testing

# Load the input CSV
input_file = 'federal_register_eos.csv'
df = pd.read_csv(input_file)

# Output data
results = []

def analyze_with_openai(text):
    prompt = (
        "Analyze the following Executive Order content. "
        "Provide a one or two word sentiment description (e.g., angry, frustrated, positive, etc). "
        "Then provide a one or two sentence summary of the content.\n\n"
        f"Content:\n{text}\n\n"
        "Respond in the format:\nSentiment: <sentiment>\nSummary: <summary>"
    )

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes government documents."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        output = response['choices'][0]['message']['content']
        lines = output.splitlines()
        sentiment = next((line.replace("Sentiment:", "").strip() for line in lines if line.startswith("Sentiment:")), "unknown")
        summary = next((line.replace("Summary:", "").strip() for line in lines if line.startswith("Summary:")), "")
        return sentiment, summary
    except Exception as e:
        return "error", f"OpenAI API error: {e}"

# Loop through each EO URL
for index, row in df.iterrows():
    eo_number = row['EO #']
    url = row['URL']
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract EO content
        content_div = soup.find('div', {'class': 'abstract'}) or soup.find('div', {'id': 'document'})
        text = content_div.get_text(strip=True, separator=' ') if content_div else ''

        # Use OpenAI to get sentiment and summary
        sentiment, summary = analyze_with_openai(text[:4000])  # Truncate to fit prompt size limit

        results.append({
            'EO Number': eo_number,
            'Content Sentiment': sentiment,
            'Summary': summary
        })

    except Exception as e:
        results.append({
            'EO Number': eo_number,
            'Content Sentiment': 'error',
            'Summary': f'Failed to process: {e}'
        })

# Write results to a new CSV
output_file = 'eo_summary_output.csv'
with open(output_file, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=['EO Number', 'Content Sentiment', 'Summary'])
    writer.writeheader()
    writer.writerows(results)

print(f"Output written to {output_file}")
