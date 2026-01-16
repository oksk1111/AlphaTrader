#!/bin/bash

# Define the command to run the dashboard
CMD="venv/bin/streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0"

while true; do
    echo "=========================================="
    echo "🚀 Starting Streamlit Dashboard..."
    echo "Time: $(date)"
    echo "=========================================="

    # Run the command
    $CMD

    # If the command exits (crashes), we wait and restart
    EXIT_CODE=$?
    echo "⚠️ Streamlit crashed with exit code $EXIT_CODE."
    echo "🔄 Restarting in 3 seconds..."
    sleep 3
done
