# Installation Guide for Running a Python Script as a Service on Ubuntu 24.04.1 LTS

## **Introduction**

This guide provides step-by-step instructions for setting up a Python script, referred to as `<script_name>`, as a service on Ubuntu 24.04.1 LTS. It will be assumed that the username is "ubuntu". It covers script installation, configuration, and service management.

---

## **1. Choose a Location for the Script**

It is recommended to store custom scripts in `/opt` for better organization.

### **Steps:**

1. Create a directory for the script:
   ```bash
   sudo mkdir -p /opt/<script_name>
   ```
2. Adjust permissions to allow the `ubuntu` user to write to the directory:
   ```bash
   sudo chown -R ubuntu:ubuntu /opt/<script_name>
   sudo chmod -R u+w /opt/<script_name>
   ```
3. Move your script and any necessary configuration files to the directory (use an FTP client if necessary):
   ```bash
   cp <script_name>.py /opt/<script_name>/
   cp config.json /opt/<script_name>/ # If applicable
   ```
4. Make the script executable:
   ```bash
   chmod +x /opt/<script_name>/<script_name>.py
   ```

---

## **2. Install Required Libraries**

Ensure `pip` is installed and then install any required libraries.

### **Steps:**

1. Update the system and install `pip`:
   ```bash
   sudo apt update
   sudo apt install -y python3-pip
   ```
2. Install the required Python libraries:
   ```bash
   pip3 install -r requirements.txt # Or list the libraries explicitly
   ```
3. Set permissions for the directory:
   ```bash
   sudo chown -R ubuntu:ubuntu /opt/<script_name>
   sudo chmod -R u+w /opt/<script_name>
   ```

---

## **3. Test the Script**

Run the script to verify it works correctly:

```bash
python3 /opt/<script_name>/<script_name>.py
```

Resolve any errors before proceeding.

---

## **4. Create a Service File**

Set up the script as a service using `systemd`.

### **Steps:**

1. Create a new service file:
   ```bash
   sudo nano /etc/systemd/system/<script_name>.service
   ```
2. Add the following content to the file:
   ```
   [Unit]
   Description=<script_name> Service
   After=network.target

   [Service]
   ExecStart=/usr/bin/python3 /opt/<script_name>/<script_name>.py
   Restart=always
   User=ubuntu
   Group=ubuntu
   Environment="PYTHONUNBUFFERED=1"
   WorkingDirectory=/opt/<script_name>
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```
3. Save and close the file.

---

## **5. Set Permissions for the Service File**

Ensure the service file has the correct permissions:

```bash
sudo chmod 644 /etc/systemd/system/<script_name>.service
```

---

## **6. Load and Enable the Service**

### **Steps:**

1. Reload the `systemd` daemon:
   ```bash
   sudo systemctl daemon-reload
   ```
2. Enable the service to start on boot:
   ```bash
   sudo systemctl enable <script_name>.service
   ```

---

## **7. Start the Service**

Start the service and ensure it is running:

```bash
sudo systemctl start <script_name>.service
```

---

## **8. Verify the Service Status**

Check the status of the service:

```bash
sudo systemctl status <script_name>.service
```

---

## **9. Monitor Logs**

To monitor the service logs in real-time:

```bash
sudo journalctl -u <script_name>.service -f
```

---

## **10. Test Automatic Restart**

1. Stop the service manually:
   ```bash
   sudo systemctl stop <script_name>.service
   ```
2. Restart it:
   ```bash
   sudo systemctl start <script_name>.service
   ```
3. Reboot the system to ensure the service starts automatically:
   ```bash
   sudo reboot
   ```

After reboot, verify the service status:

```bash
sudo systemctl status <script_name>.service
```

---

## **11. Firewall Configuration (If Applicable)**

If the script uses a specific port (e.g., `8080`), allow it through the firewall.

### **Steps:**

1. Add a rule to allow traffic on the required port:
   ```bash
   sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
   ```
2. Make the rule persistent:
   ```bash
   sudo apt install iptables-persistent -y
   sudo netfilter-persistent save
   ```

---

## **Conclusion**

Following these steps, your Python script `<script_name>` is now configured as a service on Ubuntu 24.04.1 LTS. Monitor logs and test regularly to ensure smooth operation.

