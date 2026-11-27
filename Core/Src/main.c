/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef struct
{
  int16_t shunt_raw;
  uint16_t bus_raw;
} INA219_RawSample;

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define INA219_ADDRESS      0x40U
#define INA219_CAPTURE_CONFIG 0x319BU /* 32 V bus, +/-160 mV shunt, 12-bit, single shot. */
#define INA219_SAMPLE_MS    4U
#define INA219_SAMPLE_COUNT 30000U  /* Capture duration = sample count x interval. */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
I2C_HandleTypeDef hi2c1;

/* USER CODE BEGIN PV */
volatile uint32_t ina219_i2c_error; /* Last HAL I2C error, preserved for the debugger. */
volatile HAL_StatusTypeDef ina219_status = HAL_BUSY; /* First configuration write. */
volatile HAL_StatusTypeDef ina219_config_status = HAL_BUSY; /* Configuration readback. */
volatile uint16_t ina219_config;
volatile GPIO_PinState ina219_scl_idle;
volatile GPIO_PinState ina219_sda_idle;
volatile uint32_t ina219_attempts;
volatile INA219_RawSample ina219_log[INA219_SAMPLE_COUNT];
volatile uint32_t ina219_sample_count;
volatile HAL_StatusTypeDef ina219_capture_status = HAL_BUSY;
volatile float ina219_bus_V, ina219_shunt_mV, ina219_current_mA, ina219_power_mW;
volatile float ina219_mean_current_mA, ina219_mean_power_mW;

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_I2C1_Init(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
static HAL_StatusTypeDef INA219_Trigger(void)
{
  uint8_t config[] = {INA219_CAPTURE_CONFIG >> 8, INA219_CAPTURE_CONFIG & 0xFFU};
  return HAL_I2C_Mem_Write(&hi2c1, INA219_ADDRESS << 1, 0x00U, I2C_MEMADD_SIZE_8BIT,
                           config, sizeof(config), 10U);
}

static HAL_StatusTypeDef INA219_ReadRegister(uint8_t reg, uint16_t *value)
{
  uint8_t bytes[2];
  HAL_StatusTypeDef status = HAL_I2C_Mem_Read(&hi2c1, INA219_ADDRESS << 1, reg,
                                              I2C_MEMADD_SIZE_8BIT, bytes, sizeof(bytes), 10U);
  if (status == HAL_OK)
  {
    *value = ((uint16_t)bytes[0] << 8) | bytes[1];
  }
  return status;
}

/* A reset during a read can leave the sensor waiting for clocks. */
static void INA219_RecoverBus(void)
{
  if (HAL_I2C_DeInit(&hi2c1) != HAL_OK) Error_Handler();
  GPIO_InitTypeDef gpio = {.Mode = GPIO_MODE_OUTPUT_OD, .Pull = GPIO_PULLUP,
                           .Speed = GPIO_SPEED_FREQ_LOW};
  HAL_GPIO_WritePin(INA_SDA_GPIO_Port, INA_SDA_Pin, GPIO_PIN_SET);
  HAL_GPIO_WritePin(INA_SCL_GPIO_Port, INA_SCL_Pin, GPIO_PIN_SET);
  gpio.Pin = INA_SDA_Pin;
  HAL_GPIO_Init(INA_SDA_GPIO_Port, &gpio);
  gpio.Pin = INA_SCL_Pin;
  HAL_GPIO_Init(INA_SCL_GPIO_Port, &gpio);

  for (unsigned i = 0; i < 9U && HAL_GPIO_ReadPin(INA_SDA_GPIO_Port, INA_SDA_Pin) == GPIO_PIN_RESET; ++i)
  {
    if (HAL_GPIO_ReadPin(INA_SCL_GPIO_Port, INA_SCL_Pin) == GPIO_PIN_RESET) break;
    HAL_GPIO_WritePin(INA_SCL_GPIO_Port, INA_SCL_Pin, GPIO_PIN_RESET);
    HAL_Delay(1U);
    HAL_GPIO_WritePin(INA_SCL_GPIO_Port, INA_SCL_Pin, GPIO_PIN_SET);
    HAL_Delay(1U);
  }
  if (HAL_GPIO_ReadPin(INA_SCL_GPIO_Port, INA_SCL_Pin) == GPIO_PIN_SET)
  {
    /* Generate STOP, then hand the released pins back to I2C1. */
    HAL_GPIO_WritePin(INA_SCL_GPIO_Port, INA_SCL_Pin, GPIO_PIN_RESET);
    HAL_GPIO_WritePin(INA_SDA_GPIO_Port, INA_SDA_Pin, GPIO_PIN_RESET);
    HAL_Delay(1U);
    HAL_GPIO_WritePin(INA_SCL_GPIO_Port, INA_SCL_Pin, GPIO_PIN_SET);
    HAL_Delay(1U);
    HAL_GPIO_WritePin(INA_SDA_GPIO_Port, INA_SDA_Pin, GPIO_PIN_SET);
    HAL_Delay(1U);
  }
  MX_I2C1_Init();
}

static HAL_StatusTypeDef INA219_Capture(void)
{
  double sum_current = 0.0, sum_power = 0.0;
  ina219_sample_count = 0U;
  ina219_config_status = HAL_BUSY;
  uint32_t slot = HAL_GetTick();
  for (uint32_t i = 0; i < INA219_SAMPLE_COUNT; ++i)
  {
    uint16_t bus, shunt;
    HAL_StatusTypeDef status = INA219_Trigger();
    if (i == 0U)
    {
      ina219_status = status;
      ++ina219_attempts;
      if (status != HAL_OK) return status;
      uint16_t config;
      status = INA219_ReadRegister(0x00U, &config);
      if (status == HAL_OK)
      {
        ina219_config = config;
        if (config != INA219_CAPTURE_CONFIG) status = HAL_ERROR;
      }
      ina219_config_status = status;
    }
    if (status != HAL_OK) return status;

    /* The configuration write clears CNVR. Both results stay latched after conversion. */
    do
    {
      status = INA219_ReadRegister(0x02U, &bus);
      if (status != HAL_OK) return status;
      if ((HAL_GetTick() - slot) >= INA219_SAMPLE_MS) return HAL_TIMEOUT;
    } while ((bus & 0x0002U) == 0U);
    status = INA219_ReadRegister(0x01U, &shunt);
    if (status != HAL_OK) return status;

    int16_t signed_shunt = (int16_t)shunt;
    float shunt_mV = signed_shunt * 0.01f;
    float bus_V = (bus >> 3) * 0.004f;          /* VIN- relative to GND. */
    float current_mA = signed_shunt * 0.1f;      /* 10 uV/bit and 0.1 ohm. */
    float power_mW = (bus_V + shunt_mV / 1000.0f) * current_mA;
    ina219_log[i].shunt_raw = signed_shunt;
    ina219_log[i].bus_raw = bus;
    ina219_bus_V = bus_V;
    ina219_shunt_mV = shunt_mV;
    ina219_current_mA = current_mA;
    ina219_power_mW = power_mW;
    sum_current += current_mA;
    sum_power += power_mW;
    ina219_sample_count = i + 1U;
    if (i + 1U < INA219_SAMPLE_COUNT)
    {
      slot += INA219_SAMPLE_MS;
      uint32_t now;
      do
      {
        now = HAL_GetTick();
      } while ((int32_t)(now - slot) < 0);
      if (now != slot) return HAL_TIMEOUT; /* Preserve the configured sample interval. */
    }
  }
  ina219_mean_current_mA = (float)(sum_current / INA219_SAMPLE_COUNT);
  ina219_mean_power_mW = (float)(sum_power / INA219_SAMPLE_COUNT);
  return HAL_OK;
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_I2C1_Init();
  /* USER CODE BEGIN 2 */
  HAL_Delay(1U); /* Let the released open-drain lines settle. */

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    if (ina219_capture_status != HAL_OK)
    {
      ina219_capture_status = HAL_BUSY;
      ina219_scl_idle = HAL_GPIO_ReadPin(INA_SCL_GPIO_Port, INA_SCL_Pin);
      ina219_sda_idle = HAL_GPIO_ReadPin(INA_SDA_GPIO_Port, INA_SDA_Pin);
      HAL_StatusTypeDef status = INA219_Capture();
      ina219_i2c_error = HAL_I2C_GetError(&hi2c1);
      ina219_capture_status = status;
      if (status != HAL_OK)
      {
        HAL_Delay(1000U);
        if (__HAL_I2C_GET_FLAG(&hi2c1, I2C_FLAG_BUSY) ||
            HAL_GPIO_ReadPin(INA_SDA_GPIO_Port, INA_SDA_Pin) == GPIO_PIN_RESET)
        {
          INA219_RecoverBus();
        }
      }
    }
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  if (HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_MSI;
  RCC_OscInitStruct.MSIState = RCC_MSI_ON;
  RCC_OscInitStruct.MSICalibrationValue = 0;
  RCC_OscInitStruct.MSIClockRange = RCC_MSIRANGE_8;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_MSI;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_0) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief I2C1 Initialization Function
  * @retval None
  */
static void MX_I2C1_Init(void)
{
  /* USER CODE BEGIN I2C1_Init 0 */

  /* USER CODE END I2C1_Init 0 */
  hi2c1.Instance = I2C1;
  hi2c1.Init.Timing = 0x1092232B; /* PCLK1 16 MHz, standard-mode I2C. */
  hi2c1.Init.OwnAddress1 = 0;
  hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  hi2c1.Init.OwnAddress2 = 0;
  hi2c1.Init.OwnAddress2Masks = I2C_OA2_NOMASK;
  hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&hi2c1) != HAL_OK ||
      HAL_I2CEx_ConfigAnalogFilter(&hi2c1, I2C_ANALOGFILTER_ENABLE) != HAL_OK ||
      HAL_I2CEx_ConfigDigitalFilter(&hi2c1, 0U) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN I2C1_Init 2 */

  /* USER CODE END I2C1_Init 2 */
}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOA_CLK_ENABLE();

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
