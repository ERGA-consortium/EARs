required_packages <- c("readr", "dplyr", "ggplot2", "lubridate")

for (pkg in required_packages) {
  if (!requireNamespace(pkg, quietly = TRUE)) {
    install.packages(pkg)
  }
}

library(readr)
library(dplyr)
library(ggplot2)
library(lubridate)

url <- "https://raw.githubusercontent.com/ERGA-consortium/EARs/refs/heads/main/rev/EAR_reviews.csv"
df <- read_csv(url)

# dates to Date class
df <- df %>%
  mutate(
    `Open Date` = as.Date(`Open Date`),
    `Approval Date` = as.Date(`Approval Date`)
  )

# add Approval Time categories
df <- df %>%
  mutate(
    Approval_Days = as.numeric(`Approval Date` - `Open Date`),
    `Approval Time` = case_when(
      Approval_Days >= 1  & Approval_Days <= 3  ~ "Very Fast",
      Approval_Days >= 4  & Approval_Days <= 7  ~ "Fast",
      Approval_Days >= 8  & Approval_Days <= 14 ~ "Standard",
      Approval_Days >= 15 & Approval_Days <= 21 ~ "Slight Delay",
      Approval_Days >= 22 & Approval_Days <= 28 ~ "Moderate Delay",
      Approval_Days >= 29 & Approval_Days <= 35 ~ "Extended Delay",
      Approval_Days > 35                        ~ "Severe Delay",
      TRUE                                      ~ NA_character_
    ),
    `Approval Time` = factor(`Approval Time`,
                             levels = c("Severe Delay", "Extended Delay",
                                        "Moderate Delay", "Slight Delay",
                                        "Standard", "Fast", "Very Fast"))
  )

# palette
my_colors <- c(
  "Very Fast"      = "#eff3ff",
  "Fast"           = "#9ecae1",
  "Standard"       = "#74c476",
  "Slight Delay"   = "#ffeda0",
  "Moderate Delay" = "#feb24c",
  "Extended Delay" = "#f03b20",
  "Severe Delay"   = "#bd0026"
)

# Plot
ggplot(df, aes(x = format(`Open Date`, "%Y-%m"), fill = `Approval Time`)) +
  geom_bar() +
  geom_bar(aes(x = format(`Open Date`, "%Y-%m")), fill = NA, color = "black", size = 0.2, stat = "count") +
  scale_fill_manual(values = my_colors) +
  labs(
    x = "Year.Month",
    y = "Number of Reviews",
    fill = "Approval Time"
  ) +
  theme_minimal() +
  theme(
    panel.grid.minor = element_blank(),
    panel.grid.major.x = element_blank(),
    axis.text.x = element_text(angle = 45, hjust = 1)
  )
