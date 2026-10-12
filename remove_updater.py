with open('telefonerkennung.py', 'rb') as f:
    content = f.read()

# Find and remove UPDATER_SCRIPT section
start = content.find(b"UPDATER_SCRIPT = r'''")
if start >= 0:
    # Find the end of the UPDATER_SCRIPT (next ''' followed by blank line and # =====)
    end_marker = b"'''\r\n\r\n# ============================================================\r\n# LOGGING"
    end = content.find(end_marker, start)
    if end >= 0:
        end += len(end_marker)
        content = content[:start] + content[end:]
        with open('telefonerkennung.py', 'wb') as f:
            f.write(content)
        print("UPDATER_SCRIPT removed")
    else:
        print("End marker not found")
else:
    print("UPDATER_SCRIPT not found")