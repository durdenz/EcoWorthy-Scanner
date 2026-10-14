## Setup:
Run this setup command to load the dependencies for the project:
```
sudo apt install python3-venv bluez
```

Run the command below to create the python env for the project.
```
python3 -m venv ~/ecoenv && ~/ecoenv/bin/pip install bleak
```

Run the scan below to find the mac addresses of your batteries.
The addresses will be in the format aa:bb:cc:dd:ee:ff 
Save these addresses as you will need to add them to the command line to run the logger.
```
~/ecoenv/bin/python ecoworthy_logger.py --scan
```

## Execution:
Modify the following command use the mac addresses for your batteries (add additional batteries as needed using --battery) then run the command tp launch the logger script.
The logger script will create a csv file in ~/battery_log.csv to contain the date from each battery.
```
~/ecoenv/bin/python ecoworthy_logger.py \
  --battery house1=aa:bb:cc:dd:ee:ff \
  --battery house2=aa:bb:cc:dd:ee:ff \
  --interval 30 --csv ~/battery_log.csv \
  --signalk-host 127.0.0.1
```


## Copying the file to a shared drive (eg. rclone etc):
Getting the data into a FUSE mounted drive for remote access

Copying the whole file works fine on FUSE mounts, because it's a plain create/overwrite rather than an append. Add a cron job (crontab -e):

```
*/5 * * * * cp /home/pi/battery_log.csv /home/pi/BoatPi/battery_log.csv
```

If the network drive is offline, the copy just fails and tries again 5 minutes later, with no effect on logging. The file will grow by roughly 0.7 MB per day with two batteries at 30-second intervals, which is no concern for an SD card.

## Enabling as a Service:
## Starting at boot and recovering automatically

Save the info below in a file as /etc/systemd/system/battery-logger.service, adjusting the script path if you keep it somewhere other than your home folder:

```
[Unit]
Description=Eco-Worthy battery CSV logger
After=bluetooth.target
Wants=bluetooth.target

[Service]
User=pi
ExecStart=/home/pi/ecoenv/bin/python -u /home/pi/ecoenv/ecoworthy_logger.py --battery house1=A5:C2:37:69:78:6D --battery house2=A5:C2:37:69:8F:74 --interval 30 --csv /home/pi/battery_log.csv --signalk-host 127.0.0.1
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable it:

bash
```
sudo systemctl daemon-reload
sudo systemctl enable --now battery-logger
journalctl -u battery-logger -f   # watch the live log
```

Stop your manual run first so two copies don't compete for the Bluetooth connections.

## Optional next steps

Publish voltage, current and SOC to Signal K so they appear on your OpenPlotter dashboards.
Remove the empty temp2_c and temp3_c columns, since each battery has only one temperature sensor.
Raise the SOC logging frequency or add a "log only on change" mode if you want smaller files.

## Adding SignalK Publishing:
I've added Signal K publishing to ecoworthy_logger.py. I tested the delta builder locally with sample data and it produces the expected values, but I haven't run it against a real Signal K server.

1. Add a UDP input in Signal K (one time)

In the Signal K admin UI (usually http://<pi-address>:3000):

Go to Server → Connections → Add.
Set the data type to SignalK, the input type to UDP, and the port to 8375.
Apply the change and let the server restart.

The exact menu labels can vary slightly between versions. If port 8375 is already in use, pick another and pass it with --signalk-port.

2. Run the logger with Signal K enabled

bash
```
~/ecoenv/bin/python ecoworthy_logger.py \
  --battery house1=A5:C2:37:69:78:6D \
  --battery house2=A5:C2:37:69:8F:74 \
  --interval 30 --csv ~/battery_log.csv \
  --signalk-host 127.0.0.1
```

To use it in the systemd service, add --signalk-host 127.0.0.1 to the end of the ExecStart line.

What gets published (under electrical.batteries.house1 and house2)

voltage, current, temperature (in Kelvin, as Signal K requires)
capacity.stateOfCharge as a 0 to 1 ratio, plus capacity.remaining and capacity.nominal in joules
cycles, cells.1 to cells.4 voltages, and cellDelta

## Details to be aware of

Current sign is flipped. Signal K treats positive battery current as flowing out of the battery, while the BMS reports charging as positive, so the script negates it. Charging will show as negative in Signal K, and the CSV keeps the BMS convention.
Joules are approximate. They're calculated as Ah × 3600 × the present pack voltage.
cycles, cells.N and cellDelta are custom paths. They aren't part of the standard Signal K battery schema, so some dashboards won't offer them by default. Voltage, current, SOC and temperature will work with standard gauges.
No retries are needed for Signal K. UDP is fire-and-forget, so if the server is down the script just carries on logging to CSV.

## Checking it works: 
open Data Browser in the Signal K admin UI and look for vessels.self → electrical → batteries. If nothing appears, check the UDP connection settings first, then look for SIGNAL K ERROR lines in the logger output.

