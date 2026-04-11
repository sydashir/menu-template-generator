from pipeline import process
from pathlib import Path

def main():
    output_dir = "gemini_tests"
    
    # Reprocess Le Premier (EARLY BIRD MENU  SRQ_2.jpg)
    print("\nReprocessing Le Premier...")
    process("Menu Template/EARLY BIRD MENU  SRQ_2.jpg", output_dir)
    
    # Reprocess AMI Brunch
    print("\nReprocessing AMI Brunch...")
    process("Menu Template/AMI_brunch_Lunch_Menu.JPG", output_dir)

if __name__ == "__main__":
    main()
