with open('telefonerkennung.py', 'rb') as f:
    content = f.read()

# Find UPDATER_SCRIPT start
start = content.find(b"UPDATER_SCRIPT = r'''")
if start >= 0:
    # Find end marker
    end_marker = b"'''\r\n\r\n# ============================================================\r\n# LOGGING\r\n# ============================================================\r\ndef setup_logging():"
    end = content.find(end_marker)
    if end >= 0:
        end += len(end_marker)
        content = content[:start] + content[end:]
        with open('telefonerkennung.py', 'wb') as f:
            f.write(content)
        print("UPDATER_SCRIPT removed successfully")
    else:
        print("End marker not found")
else:
    print("UPDATER_SCRIPT not found")